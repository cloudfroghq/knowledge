"""Layered imports of src/knowledge: core, ext and http over a flat package.

The modules stay flat, so the three groups live in ``.importlinter`` rather
than in directories: core (indexing and querying vaults, no transport, and the
declarative registry of extensions), ext (git, memory reads, review policy,
Confluence, graphify targets, the reindex webhook, auth-flow documents, the
memory-bridge writes) and
http (auth, the server, the command line). Two independent mechanisms go red
the moment a commit points an arrow the wrong way, deliberately not sharing
code, so a bug in one does not blind the other:

* ``test_the_import_linter_contract_is_kept`` runs the exact contracts the
  ``Layered architecture: import-linter (ADR-022)`` pipeline step runs, through
  import-linter's own Python API — the same tool, not a reimplementation of it.
* ``test_layer_imported_in_isolation_does_not_pull_a_higher_or_sibling_layer``
  imports one module of ``knowledge`` at a time, in a fresh subprocess, and
  fails if any module that is not on its allowed-dependency list shows up in
  ``sys.modules`` afterwards. This does not use import-linter or grimp at all:
  it is a plain Python import, so it still catches a regression if the
  ``.importlinter`` contracts are ever broken, deleted, or misconfigured.

The subprocess is not optional. Other test modules in this suite import
``knowledge.server`` (directly or through fixtures) before this file's tests
run, which would leave ``knowledge.server`` sitting in ``sys.modules`` already
— that would make a leak from, say, ``knowledge.config`` invisible to an
in-process check. A fresh interpreter per case is the only way "importing this
module alone" means what it says.

``_ALLOWED_DIRECT`` is the same graph committed to ``.importlinter``, written
here independently: errors, repos, ports, contract, graph and usage
import nothing internal, and neither do pointers and chunker; stores imports
errors and ports; gitsync imports ports;
memory_http imports errors, ports, contract and settings; memory_bridge
imports errors, contract and settings; jira_client imports errors and
settings; judges imports ports, contract and settings; jira_sync imports errors, extensions, jira_client, memory_bridge
and settings; settings imports
errors and contract; extensions imports settings and contract; principals imports
config, errors and settings; flows imports errors; profiles imports errors and contract; embeddings
imports errors and ports; vault_source imports ports; config (the legacy
facade) imports settings; auth imports errors and principals; review
imports only config, confluence_sync imports config and errors, and webhook imports config, contract,
errors, principals, settings and stores; indexer imports errors, ports, contract, graph,
pointers, chunker and the embeddings, stores and vault_source adapters; vault_lint imports errors, graph and settings;
rerank imports pointers, ports, settings and usage; registry
imports errors, indexer, rerank, settings, usage, ports and the adapters it composes;
graph_targets imports settings and repos; contexts imports errors and repos; server imports the layers below it
(but loads flows, confluence_sync, memory_bridge, jira_sync and judges only through importlib, so
importing it never pulls them);
cli imports server, settings, principals, stores, registry, errors, usage, vault_lint, contexts and
repos. Verified by mapping
every ``from knowledge`` / ``import knowledge`` line in src/knowledge/*.py —
no cycles.
"""

from __future__ import annotations

import configparser
import subprocess
import sys
from pathlib import Path

import pytest

from importlinter import cli as importlinter_cli

REPO_ROOT = Path(__file__).resolve().parent.parent
IMPORTLINTER_CONFIG = REPO_ROOT / ".importlinter"

# Direct internal dependencies, one entry per src/knowledge/ module that isn't
# a trivial entry point (__init__.py is empty; __main__.py only wires
# knowledge.cli.main and is not a layer of its own).
_ALLOWED_DIRECT: dict[str, frozenset[str]] = {
    "knowledge.errors": frozenset(),
    "knowledge.repos": frozenset(),
    "knowledge.ports": frozenset(),
    "knowledge.contract": frozenset(),
    "knowledge.stores": frozenset({"knowledge.errors", "knowledge.ports"}),
    "knowledge.embeddings": frozenset({"knowledge.errors", "knowledge.ports"}),
    "knowledge.vault_source": frozenset({"knowledge.ports"}),
    "knowledge.settings": frozenset({"knowledge.errors", "knowledge.contract"}),
    "knowledge.profiles": frozenset({"knowledge.errors", "knowledge.contract"}),
    "knowledge.extensions": frozenset({"knowledge.settings", "knowledge.contract"}),
    "knowledge.principals": frozenset({"knowledge.config", "knowledge.errors", "knowledge.settings"}),
    "knowledge.flows": frozenset({"knowledge.errors"}),
    "knowledge.pointers": frozenset(),
    "knowledge.chunker": frozenset(),
    "knowledge.config": frozenset({"knowledge.settings"}),
    "knowledge.graph": frozenset(),
    "knowledge.usage": frozenset(),
    "knowledge.gitsync": frozenset({"knowledge.ports"}),
    "knowledge.auth": frozenset({"knowledge.errors", "knowledge.principals"}),
    "knowledge.review": frozenset({"knowledge.config"}),
    "knowledge.confluence_sync": frozenset({"knowledge.config", "knowledge.errors"}),
    "knowledge.indexer": frozenset(
        {
            "knowledge.errors",
            "knowledge.ports",
            "knowledge.contract",
            "knowledge.graph",
            "knowledge.pointers",
            "knowledge.chunker",
            "knowledge.embeddings",
            "knowledge.stores",
            "knowledge.vault_source",
        }
    ),
    "knowledge.rerank": frozenset(
        {"knowledge.pointers", "knowledge.ports", "knowledge.settings", "knowledge.usage"}
    ),
    "knowledge.registry": frozenset(
        {
            "knowledge.errors",
            "knowledge.indexer",
            "knowledge.rerank",
            "knowledge.settings",
            "knowledge.usage",
            "knowledge.ports",
            "knowledge.embeddings",
            "knowledge.stores",
            "knowledge.vault_source",
        }
    ),
    "knowledge.graph_targets": frozenset({"knowledge.settings", "knowledge.repos"}),
    "knowledge.contexts": frozenset({"knowledge.errors", "knowledge.repos"}),
    "knowledge.vault_lint": frozenset({"knowledge.errors", "knowledge.graph", "knowledge.settings"}),
    "knowledge.cli": frozenset(
        {
            "knowledge.server",
            "knowledge.settings",
            "knowledge.principals",
            "knowledge.stores",
            "knowledge.registry",
            "knowledge.errors",
            "knowledge.usage",
            "knowledge.vault_lint",
            "knowledge.contexts",
            "knowledge.repos",
        }
    ),
    "knowledge.webhook": frozenset(
        {
            "knowledge.config",
            "knowledge.contract",
            "knowledge.errors",
            "knowledge.principals",
            "knowledge.settings",
            "knowledge.stores",
        }
    ),
    "knowledge.memory_http": frozenset(
        {"knowledge.errors", "knowledge.ports", "knowledge.contract", "knowledge.settings"}
    ),
    "knowledge.memory_bridge": frozenset({"knowledge.errors", "knowledge.contract", "knowledge.settings"}),
    "knowledge.jira_client": frozenset({"knowledge.errors", "knowledge.settings"}),
    "knowledge.judges": frozenset({"knowledge.ports", "knowledge.contract", "knowledge.settings"}),
    "knowledge.jira_sync": frozenset(
        {
            "knowledge.errors",
            "knowledge.extensions",
            "knowledge.jira_client",
            "knowledge.memory_bridge",
            "knowledge.settings",
        }
    ),
    "knowledge.server": frozenset(
        {
            "knowledge.errors",
            "knowledge.settings",
            "knowledge.config",
            "knowledge.extensions",
            "knowledge.profiles",
            "knowledge.pointers",
            "knowledge.review",
            "knowledge.indexer",
            "knowledge.registry",
            "knowledge.rerank",
            "knowledge.ports",
            "knowledge.stores",
            "knowledge.usage",
            "knowledge.webhook",
            "knowledge.gitsync",
            "knowledge.memory_http",
            "knowledge.auth",
            "knowledge.vault_lint",
        }
    ),
}


def _transitive_closure(module: str) -> frozenset[str]:
    seen: set[str] = set()
    frontier = list(_ALLOWED_DIRECT[module])
    while frontier:
        dep = frontier.pop()
        if dep in seen:
            continue
        seen.add(dep)
        frontier.extend(_ALLOWED_DIRECT[dep])
    return frozenset(seen)


def _forbidden_modules(module: str) -> frozenset[str]:
    """Every other module this one must never end up importing, direct or not.

    This is deliberately stricter than "no import of a higher layer":
    modules declared independent siblings in .importlinter (e.g. indexer and
    webhook, both direct children of server) are forbidden from importing each
    other too, and this catches that the same way.
    """
    allowed = _transitive_closure(module) | {module}
    return frozenset(m for m in _ALLOWED_DIRECT if m not in allowed)


def test_the_import_linter_contract_is_kept() -> None:
    """The exact contract the pipeline's import-linter step runs, run the same way.

    Calls import-linter's own public entry point (importlinter.cli.lint_imports)
    against the committed .importlinter — not a reimplementation, the real
    check the CI step performs. See test_the_pipeline_names_the_new_gate below
    for proof this is actually wired into the pull-request pipeline.
    """
    assert IMPORTLINTER_CONFIG.is_file(), f"missing contract file: {IMPORTLINTER_CONFIG}"
    exit_code = importlinter_cli.lint_imports(
        config_filename=str(IMPORTLINTER_CONFIG),
        no_cache=True,
        no_logo=True,
    )
    assert exit_code == 0, "an import contract is broken against the committed tree"


def test_the_pipeline_names_the_new_gate() -> None:
    """Guards the wiring test_pipeline_contract.py does not check by name.

    This test fails if an edit drops the import-linter job's name or its
    `uv run lint-imports` invocation from ring1.yml.
    """
    import yaml

    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ring1.yml").read_text())
    jobs = [job for job in workflow["jobs"].values() if job.get("name") == "Layered architecture: import-linter (ADR-022)"]
    assert jobs, "ring1.yml does not define the import-linter job"

    run_lines = [step["run"] for step in jobs[0]["steps"] if isinstance(step.get("run"), str)]
    assert any("lint-imports" in line for line in run_lines), (
        "the job exists but does not run lint-imports"
    )


@pytest.mark.parametrize(
    "module_name",
    sorted(m for m in _ALLOWED_DIRECT if _forbidden_modules(m)),
)
def test_layer_imported_in_isolation_does_not_pull_a_higher_or_sibling_layer(module_name: str) -> None:
    """Import one module of `knowledge` alone; nothing forbidden may follow it in.

    Runs as a subprocess against a fresh interpreter — see the module docstring
    for why an in-process check would miss a leak that another test already
    caused. A module with nothing forbidden (nothing outside its own closure)
    is excluded from parametrization (see _forbidden_modules).
    """
    forbidden = sorted(_forbidden_modules(module_name))
    probe = (
        "import sys\n"
        f"import {module_name}\n"
        f"forbidden = {forbidden!r}\n"
        "leaked = sorted(m for m in forbidden if m in sys.modules)\n"
        "print('LEAKED=' + ','.join(leaked))\n"
        "sys.exit(1 if leaked else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"importing {module_name} alone pulled in a forbidden module — "
        f"{result.stdout.strip()}\n{result.stderr}"
    )


def test_every_internal_module_is_covered_by_the_dependency_map() -> None:
    """A module added to src/knowledge/ without updating this map checks nothing.

    Keeps _ALLOWED_DIRECT honest against the tree instead of the tree's module
    list slowly drifting away from what this file believes it is testing.
    """
    src_modules = {
        f"knowledge.{path.stem}"
        for path in (REPO_ROOT / "src" / "knowledge").glob("*.py")
        if path.stem not in {"__init__", "__main__"}
    }
    assert src_modules == set(_ALLOWED_DIRECT), (
        "src/knowledge/*.py and _ALLOWED_DIRECT have drifted apart: "
        f"only on disk: {sorted(src_modules - set(_ALLOWED_DIRECT))}, "
        f"only in the map: {sorted(set(_ALLOWED_DIRECT) - src_modules)}"
    )


# The three groups, written independently of .importlinter so the test below
# compares two sources instead of reading one back.
_CORE = frozenset(
    {
        "knowledge.errors",
        "knowledge.ports",
        "knowledge.contract",
        "knowledge.settings",
        "knowledge.config",
        "knowledge.graph",
        "knowledge.usage",
        "knowledge.profiles",
        "knowledge.pointers",
        "knowledge.chunker",
        "knowledge.principals",
        "knowledge.extensions",
        "knowledge.embeddings",
        "knowledge.stores",
        "knowledge.vault_source",
        "knowledge.indexer",
        "knowledge.vault_lint",
        "knowledge.rerank",
        "knowledge.registry",
    }
)
_EXT = frozenset(
    {
        "knowledge.repos",
        "knowledge.gitsync",
        "knowledge.memory_http",
        "knowledge.memory_bridge",
        "knowledge.review",
        "knowledge.confluence_sync",
        "knowledge.graph_targets",
        "knowledge.webhook",
        "knowledge.flows",
        "knowledge.jira_client",
        "knowledge.jira_sync",
        "knowledge.judges",
        "knowledge.contexts",
    }
)
_HTTP = frozenset({"knowledge.auth", "knowledge.server", "knowledge.cli"})
_TRANSPORT = frozenset({"mcp", "starlette", "uvicorn"})


def _contracts() -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser()
    parser.read(IMPORTLINTER_CONFIG)
    prefix = "importlinter:contract:"
    return {
        section[len(prefix) :]: dict(parser[section])
        for section in parser.sections()
        if section.startswith(prefix)
    }


def _modules(value: str) -> frozenset[str]:
    return frozenset(line.strip() for line in value.splitlines() if line.strip())


def test_the_contract_file_declares_the_four_contracts() -> None:
    """The core, ext and http shape is four contracts; a dropped one checks nothing."""
    contracts = _contracts()

    assert set(contracts) == {"layers", "core-isolated", "ext-below-http", "core-no-transport"}
    assert contracts["layers"]["type"] == "layers"
    for name in ("core-isolated", "ext-below-http", "core-no-transport"):
        assert contracts[name]["type"] == "forbidden", name


def test_the_groups_partition_every_module() -> None:
    """Each module of src/knowledge belongs to exactly one of core, ext and http."""
    assert not (_CORE & _EXT) and not (_CORE & _HTTP) and not (_EXT & _HTTP)
    assert _CORE | _EXT | _HTTP == set(_ALLOWED_DIRECT)


def test_the_forbidden_contracts_name_the_groups() -> None:
    """The forbidden contracts list exactly the groups this file declares."""
    contracts = _contracts()

    assert _modules(contracts["core-isolated"]["source_modules"]) == _CORE
    assert _modules(contracts["core-isolated"]["forbidden_modules"]) == _EXT | _HTTP
    assert _modules(contracts["ext-below-http"]["source_modules"]) == _EXT
    assert _modules(contracts["ext-below-http"]["forbidden_modules"]) == _HTTP
    assert _modules(contracts["core-no-transport"]["source_modules"]) == _CORE
    assert _modules(contracts["core-no-transport"]["forbidden_modules"]) == _TRANSPORT


def test_the_dependency_map_respects_the_groups() -> None:
    """The independent map agrees: no core module reaches ext or http, no ext reaches http."""
    for module in _CORE:
        assert not (_transitive_closure(module) & (_EXT | _HTTP)), module
    for module in _EXT:
        assert not (_transitive_closure(module) & _HTTP), module


#: The order of the ``layers`` contract, top to bottom, written independently
#: of ``.importlinter``. A name in parentheses is reserved before its module
#: exists; import-linter treats it as optional.
_LAYERS: tuple[str, ...] = (
    "cli",
    "server",
    "auth | webhook | graph_targets | jira_sync",
    "confluence_sync | review | gitsync | memory_http | memory_bridge | jira_client | flows | judges | contexts",
    "registry",
    "indexer | vault_lint | rerank",
    "embeddings | stores | vault_source | usage | graph | profiles | pointers | chunker | principals | extensions",
    "config",
    "settings",
    "ports | errors | repos | contract",
)


def _layer_names(layers: tuple[str, ...]) -> list[str]:
    return [name.strip() for layer in layers for name in layer.split("|")]


def test_the_layers_contract_is_the_declared_order() -> None:
    """``.importlinter`` lists the layers exactly as this file declares them."""
    declared = tuple(line.strip() for line in _contracts()["layers"]["layers"].splitlines() if line.strip())

    assert declared == _LAYERS


def test_every_module_is_a_layer_and_only_absent_modules_are_optional() -> None:
    """A required layer exists on disk; an optional one does not yet.

    import-linter skips an optional layer that is missing, so a module that
    exists while its layer is still in parentheses would be checked less
    than its neighbours: its first commit drops the parentheses.
    """
    names = _layer_names(_LAYERS)
    required = {f"knowledge.{n}" for n in names if not n.startswith("(")}
    optional = {f"knowledge.{n.strip('()')}" for n in names if n.startswith("(")}

    assert len(names) == len(set(names))
    assert required == set(_ALLOWED_DIRECT)
    assert not optional & set(_ALLOWED_DIRECT)
