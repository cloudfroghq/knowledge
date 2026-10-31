"""Wikilink resolution: path, path tail and stem, with aliases, headings and escapes.

The resolver never asks the file system whether a target exists: it reads the
vault's file list through ``graph.LinkIndex``, case-folded and NFC-normalised,
so it resolves the same on a case-sensitive Linux file system as on macOS.

The end-to-end cases run on ``tests/fixtures/vaults/links/``, a vault of its
own: the golden vault (``tests/fixtures/vault/``) keeps its nine edges and its
goldens untouched.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import unicodedata
from pathlib import Path
from typing import Any

import pytest
import yaml

from knowledge import server, settings, vault_lint
from knowledge.embeddings import NullEmbeddings
from knowledge.errors import E_WIKILINK_UNRESOLVED
from knowledge.graph import RESOLVER_VERSION, LinkGraph, LinkIndex, parse_target, resolve_link
from knowledge.indexer import VaultIndexer
from knowledge.registry import VaultRegistry
from knowledge.stores import InMemoryVectorStore
from knowledge.vault_source import DirectoryVaultSource

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LINKS_VAULT = FIXTURES / "vaults" / "links"
GOLDEN_VAULT = FIXTURES / "vault"

HUB, ALPHA_NOTE, BETA_NOTE = "Alpha/Hub.md", "Alpha/Note.md", "Beta/Note.md"
OTHER, GLOSSARY = "Beta/Other.md", "Shared/Glossary.md"

#: The six edges of the links fixture, written out by hand from its bodies.
LINKS_EDGES = {
    (HUB, ALPHA_NOTE),
    (HUB, BETA_NOTE),
    (ALPHA_NOTE, HUB),
    (OTHER, BETA_NOTE),
    (OTHER, GLOSSARY),
    (GLOSSARY, HUB),
}


def _edges(graph: LinkGraph) -> set[tuple[str, str]]:
    return {(source, target) for source in sorted(graph._outgoing) for target in graph.outgoing(source)}


def _indexer(vault: Path, state: Path) -> VaultIndexer:
    return VaultIndexer(
        source=DirectoryVaultSource(vault), store=InMemoryVectorStore(), embedder=NullEmbeddings(), state_dir=state
    )


# --- parsing ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "target"),
    [
        ("Note", "Note"),
        ("Alpha/Hub|the hub", "Alpha/Hub"),
        ("Beta/Note\\|Beta", "Beta/Note"),
        ("Note#Details", "Note"),
        ("Shared/Glossary#Terms|terms", "Shared/Glossary"),
        ("Note^block-1", "Note"),
        ("Beta/Note.md", "Beta/Note"),
        ("Beta\\Note", "Beta/Note"),
        ("  Note  ", "Note"),
        ("Diagram.canvas", "Diagram.canvas"),
        ("#Heading", None),
        ("|alias only", None),
    ],
)
def test_parse_target(raw: str, target: str | None) -> None:
    assert parse_target(raw) == target


# --- resolution over a file list -------------------------------------------------


def _index(*paths: str) -> LinkIndex:
    return LinkIndex.build(paths)


def test_an_exact_path_wins_over_a_stem_elsewhere() -> None:
    index = _index("A/Note.md", "B/Note.md")

    assert resolve_link("B/Note", index, "A/Other.md") == "B/Note.md"


def test_a_path_tail_resolves_to_the_file_ending_with_it() -> None:
    index = _index("SAAS/Modules/Design System/Design System.md", "Other/Design System.md")

    assert resolve_link("Modules/Design System/Design System", index, "Other/x.md") == (
        "SAAS/Modules/Design System/Design System.md"
    )


def test_an_ambiguous_stem_resolves_by_the_deepest_shared_directory() -> None:
    index = _index("SAAS/Modules/Design System/Design System.md", "Work/Design System.md")

    assert resolve_link("Design System", index, "SAAS/Modules/Design System/Pendientes.md") == (
        "SAAS/Modules/Design System/Design System.md"
    )
    assert resolve_link("Design System", index, "Work/Notes/x.md") == "Work/Design System.md"


def test_an_ambiguous_stem_without_shared_directories_takes_the_smallest_path() -> None:
    index = _index("Zeta/Note.md", "Alpha/Note.md")

    assert resolve_link("Note", index, "Root.md") == "Alpha/Note.md"


def test_names_differing_only_in_case_resolve_by_prefix_never_by_the_file_system() -> None:
    """``Foo.md`` and ``foo.md`` coexist on Linux; the resolver picks by directory, then by path."""
    index = _index("One/Foo.md", "Two/foo.md")

    assert resolve_link("foo", index, "Two/x.md") == "Two/foo.md"
    assert resolve_link("FOO", index, "One/x.md") == "One/Foo.md"
    same_dir = _index("Dir/Foo.md", "Dir/foo.md")
    assert resolve_link("Dir/FOO", same_dir, "Elsewhere.md") == "Dir/Foo.md"


def test_composed_and_decomposed_accents_are_the_same_name() -> None:
    decomposed = unicodedata.normalize("NFD", "Café.md")
    index = _index(f"Menu/{decomposed}")

    assert resolve_link(unicodedata.normalize("NFC", "Café"), index, "x.md") == f"Menu/{decomposed}"


def test_a_link_to_itself_or_to_nothing_is_no_edge() -> None:
    index = _index("Hub.md", "Diagram.canvas")

    assert resolve_link("Hub#Intro", index, "Hub.md") is None
    assert resolve_link("Diagram.canvas", index, "Hub.md") is None
    assert resolve_link("Missing", index, "Hub.md") is None


# --- the links fixture through the real indexer ------------------------------------


def test_the_links_fixture_has_exactly_its_six_edges(tmp_path: Path) -> None:
    indexer = _indexer(LINKS_VAULT, tmp_path)
    indexer.index()

    assert _edges(indexer.graph) == LINKS_EDGES


def test_the_edges_do_not_depend_on_whether_a_file_changed(tmp_path: Path) -> None:
    indexer = _indexer(LINKS_VAULT, tmp_path)
    indexer.index()
    first = _edges(indexer.graph)
    stats = indexer.index()

    assert stats["unchanged"] == stats["files"]
    assert _edges(indexer.graph) == first


def test_the_golden_vault_keeps_its_nine_edges(tmp_path: Path) -> None:
    indexer = _indexer(GOLDEN_VAULT, tmp_path)
    indexer.index()

    assert len(_edges(indexer.graph)) == 9


def test_the_graph_records_its_resolver_version(tmp_path: Path) -> None:
    indexer = _indexer(LINKS_VAULT, tmp_path)
    indexer.index()

    assert json.loads((tmp_path / "link_graph.json").read_text())["resolver"] == RESOLVER_VERSION


def test_a_graph_saved_by_another_resolver_is_re_registered_on_load(tmp_path: Path) -> None:
    """An index built before a resolver change serves the new edges without a reindex, weights kept."""
    indexer = _indexer(LINKS_VAULT, tmp_path)
    indexer.index()
    graph_file = tmp_path / "link_graph.json"
    stale = json.loads(graph_file.read_text())
    stale.pop("resolver")
    stale["outgoing"] = {HUB: [BETA_NOTE]}
    stale["incoming"] = {BETA_NOTE: [HUB]}
    stale["weights"] = {"||".join(sorted((HUB, BETA_NOTE))): 1.5}
    graph_file.write_text(json.dumps(stale))

    reopened = _indexer(LINKS_VAULT, tmp_path)
    reopened.load_graph()

    assert _edges(reopened.graph) == LINKS_EDGES
    assert reopened.graph.weight(HUB, BETA_NOTE) == 1.5
    saved = json.loads(graph_file.read_text())
    assert saved["resolver"] == RESOLVER_VERSION
    assert saved["weights"] == {"||".join(sorted((HUB, BETA_NOTE))): 1.5}


def test_a_graph_of_the_current_resolver_is_loaded_as_saved(tmp_path: Path) -> None:
    indexer = _indexer(LINKS_VAULT, tmp_path)
    indexer.index()
    graph_file = tmp_path / "link_graph.json"
    saved = json.loads(graph_file.read_text())
    saved["outgoing"] = {HUB: [BETA_NOTE]}
    saved["incoming"] = {BETA_NOTE: [HUB]}
    graph_file.write_text(json.dumps(saved))

    reopened = _indexer(LINKS_VAULT, tmp_path)
    reopened.load_graph()

    assert _edges(reopened.graph) == {(HUB, BETA_NOTE)}


# --- the linter agrees with the graph ----------------------------------------------


def _unresolved(root: Path) -> list[tuple[str, str]]:
    report = vault_lint.lint(root)
    return [(e["file"], e["target"]) for e in report.errors if e["code"] == E_WIKILINK_UNRESOLVED]


def test_the_linter_reports_parsed_targets_once_per_file() -> None:
    assert _unresolved(LINKS_VAULT) == [(HUB, "Diagram.canvas"), (HUB, "Missing Note")]


def test_a_link_to_an_existing_attachment_is_not_a_finding(tmp_path: Path) -> None:
    vault = tmp_path / "links"
    shutil.copytree(LINKS_VAULT, vault)
    (vault / "Shared" / "Diagram.canvas").write_text("{}")

    assert _unresolved(vault) == [(HUB, "Missing Note")]


def test_the_golden_vault_still_lints_clean() -> None:
    report = vault_lint.lint(GOLDEN_VAULT)

    assert (report.ok, len(report.errors), len(report.warnings)) == (True, 0, 0)


# --- graph tools over the links vault, through the registry -------------------------


@pytest.fixture()
def links_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> VaultRegistry:
    vault = tmp_path / "links"
    shutil.copytree(LINKS_VAULT, vault)
    vaults_file = tmp_path / "vaults.yaml"
    entries = [{"slug": "links", "path": str(vault), "default": True}]
    vaults_file.write_text(yaml.safe_dump({"version": 1, "vaults": entries}, sort_keys=False))
    root = tmp_path / "root"
    root.mkdir()
    env = {
        "KNOWLEDGE_HOME": str(tmp_path / "home"),
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
        "KNOWLEDGE_VAULTS": str(vaults_file),
    }
    registry = VaultRegistry.from_settings(settings.load_settings(env, project_root=root))
    monkeypatch.setattr(server, "_registry", registry)
    return registry


def test_get_related_follows_the_resolved_edges(links_vault: VaultRegistry) -> None:
    """``Alpha/Note`` links the hub and the hub links it: one row, direction ``both``."""
    rows = asyncio.run(server.get_related(HUB, limit=10))

    assert [(row["file_path"], row["direction"]) for row in rows] == [
        (ALPHA_NOTE, "both"),
        (BETA_NOTE, "outgoing"),
        (GLOSSARY, "incoming"),
    ]
    assert all(row["weight"] == 1.0 for row in rows)


@pytest.mark.parametrize("document", sorted({path for edge in LINKS_EDGES for path in edge}))
def test_get_related_never_repeats_a_document(links_vault: VaultRegistry, document: str) -> None:
    """Every neighbour comes once, whatever the directions linking it, and the rows match the edges."""
    rows = asyncio.run(server.get_related(document, limit=200))
    outgoing = {b for a, b in LINKS_EDGES if a == document}
    incoming = {a for a, b in LINKS_EDGES if b == document}

    assert len({row["file_path"] for row in rows}) == len(rows)
    assert {row["file_path"]: row["direction"] for row in rows} == {
        **dict.fromkeys(outgoing - incoming, "outgoing"),
        **dict.fromkeys(incoming - outgoing, "incoming"),
        **dict.fromkeys(outgoing & incoming, "both"),
    }


def test_find_path_walks_the_resolved_edges(links_vault: VaultRegistry) -> None:
    result = asyncio.run(server.find_path(OTHER, HUB))

    assert result["found"] is True
    assert result["length"] == 2


# --- the graph file is replaced atomically -----------------------------------------


def test_a_failed_save_leaves_the_saved_graph_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph_file = tmp_path / "link_graph.json"
    graph = LinkGraph()
    graph.set_links(HUB, [BETA_NOTE])
    graph.save(graph_file)
    saved = graph_file.read_text()

    graph.set_links(HUB, [])

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("knowledge.graph.os.replace", boom)
    with pytest.raises(OSError):
        graph.save(graph_file)

    assert graph_file.read_text() == saved


# --- what a saved graph holds, whatever its whitespace -----------------------------


def _learned_graph(tmp_path: Path) -> LinkGraph:
    """The links fixture's graph with three learned weights, one of them on a pair that is not an edge."""
    indexer = _indexer(LINKS_VAULT, tmp_path)
    indexer.index()
    graph = indexer.graph
    assert graph.reinforce(HUB, ALPHA_NOTE, 3)
    assert graph.reinforce(OTHER, GLOSSARY, 40)
    assert graph.reinforce(HUB, OTHER, 7)  # no edge: save() prunes it
    return graph


def _state(graph: LinkGraph) -> tuple[Any, ...]:
    return (graph._outgoing, graph._incoming, graph._weights, graph.resolver)


def test_a_saved_graph_loads_back_as_it_was_saved(tmp_path: Path) -> None:
    graph = _learned_graph(tmp_path / "state")
    graph_file = tmp_path / "link_graph.json"

    graph.save(graph_file)
    loaded = LinkGraph.load(graph_file)

    assert loaded._outgoing == graph._outgoing and loaded._incoming == graph._incoming
    assert loaded.resolver == RESOLVER_VERSION
    assert loaded._weights == {
        "||".join(sorted((HUB, ALPHA_NOTE))): round(graph.weight(HUB, ALPHA_NOTE), 4),
        "||".join(sorted((OTHER, GLOSSARY))): round(graph.weight(OTHER, GLOSSARY), 4),
    }
    data = json.loads(graph_file.read_text())
    assert list(data) == ["resolver", "outgoing", "incoming", "weights"]
    assert all(targets == sorted(targets) for targets in data["outgoing"].values())
    assert all(sources == sorted(sources) for sources in data["incoming"].values())


@pytest.mark.parametrize("indent", [2, None], ids=["indented", "compact"])
def test_a_graph_file_loads_the_same_in_either_format(tmp_path: Path, indent: int | None) -> None:
    """Files written before and after the compact format load to the same graph."""
    graph = _learned_graph(tmp_path / "state")
    reference = tmp_path / "link_graph.json"
    graph.save(reference)
    data = json.loads(reference.read_text())
    rewritten = tmp_path / "rewritten.json"
    separators = None if indent else (",", ":")
    rewritten.write_text(json.dumps(data, ensure_ascii=False, indent=indent, separators=separators))

    assert _state(LinkGraph.load(rewritten)) == _state(LinkGraph.load(reference))


def test_the_linter_skips_dot_directories_when_looking_for_attachments(tmp_path: Path) -> None:
    (tmp_path / "Note.md").write_text("[[Pic.png]] [[Hidden.png]]\n")
    (tmp_path / "Pic.png").write_bytes(b"")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "Hidden.png").write_bytes(b"")

    assert _unresolved(tmp_path) == [("Note.md", "Hidden.png")]
