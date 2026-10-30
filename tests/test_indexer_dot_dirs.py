"""The indexer reads the notes ``vault-lint`` reads: nothing under a dot-prefixed directory.

A vault that carries its context packages under ``Contexts/<app>/`` holds two
kinds of Markdown there: ``CLAUDE.md``, a note a person reads and a search
should find, and the ``.claude/`` tree (agents, commands, skills), tooling an
AI client loads and no search should return. The rule that separates them is
the one ``vault-lint`` already walks with: a path that crosses a segment
starting with ``.`` is not a note.

The golden vault holds no dot-prefixed entry, so the rule cannot move a
golden; ``test_the_golden_vault_has_no_dot_prefixed_entry`` keeps it that way.
Every case works on ``tests/fixtures/vaults/contexts`` or a copy of it.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from knowledge import registry, vault_lint, webhook
from knowledge.registry import parse_vaults_file
from knowledge.vault_source import DirectoryVaultSource, is_hidden

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTEXTS_VAULT = REPO_ROOT / "tests" / "fixtures" / "vaults" / "contexts"
GOLDEN_VAULT = REPO_ROOT / "tests" / "fixtures" / "vault"

NOTES = {
    "Contexts/ledger-api/CLAUDE.md",
    "Contexts/ledger-web/CLAUDE.md",
    "Contexts/notes-cli/CLAUDE.md",
    "Runbooks/Ledger Runbook.md",
    "Services/Ledger Service.md",
}


def _relative(paths: list[Path], root: Path) -> list[str]:
    return [path.relative_to(root).as_posix() for path in paths]


def test_the_source_skips_every_dot_prefixed_segment() -> None:
    files = _relative(DirectoryVaultSource(CONTEXTS_VAULT).markdown_files(), CONTEXTS_VAULT)

    assert set(files) == NOTES
    assert files == sorted(files), "the walk keeps its stable order"
    assert (CONTEXTS_VAULT / "Contexts" / "ledger-api" / ".claude" / "agents" / "x.md").is_file(), (
        "the fixture must hold the dot-directory note the walk skips, or this case checks nothing"
    )


def test_the_indexer_and_the_linter_walk_the_same_notes() -> None:
    source = DirectoryVaultSource(CONTEXTS_VAULT).markdown_files()

    assert source == vault_lint._markdown_files(CONTEXTS_VAULT)


@pytest.mark.parametrize(
    ("relative", "hidden"),
    [
        ("Contexts/app/CLAUDE.md", False),
        ("Contexts/app/.claude/agents/x.md", True),
        (".obsidian/workspace.md", True),
        ("Notes/.draft.md", True),
        ("Notes/v1.2 release.md", False),
    ],
)
def test_a_segment_is_hidden_only_when_it_starts_with_a_dot(relative: str, hidden: bool) -> None:
    assert is_hidden(Path(relative)) is hidden


def test_the_golden_vault_has_no_dot_prefixed_entry() -> None:
    """The guard that keeps the indexer change away from every golden."""
    offenders = [
        path.relative_to(GOLDEN_VAULT).as_posix()
        for path in GOLDEN_VAULT.rglob("*")
        if is_hidden(path.relative_to(GOLDEN_VAULT))
    ]
    assert offenders == []


def _served_copy(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    shutil.copytree(CONTEXTS_VAULT, vault)
    obsidian = vault / ".obsidian"
    obsidian.mkdir()
    (obsidian / "workspace.md").write_text("# Workspace\n\nEditor state, never a note.\n", encoding="utf-8")
    return vault


def test_a_reindex_stores_the_context_notes_and_nothing_under_a_dot_directory(tmp_path: Path) -> None:
    vault = _served_copy(tmp_path)
    manifest = tmp_path / "vaults.yaml"
    manifest.write_text(
        f"version: 1\ndata_dir: {tmp_path / 'data'}\nvaults:\n  - slug: contexts\n    path: {vault}\n    default: true\n",
        encoding="utf-8",
    )
    (spec,), _ = parse_vaults_file(manifest, {"KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words"})

    runtime = registry.build_runtime(spec)
    stats = runtime.indexer.index(force=True)
    stored = set(runtime.indexer.store.file_hashes())

    assert stored == NOTES
    assert stats["files"] == len(NOTES)


def test_health_staleness_ignores_a_note_under_a_dot_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _served_copy(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    indexed_at = time.time() - 60
    for path in vault.rglob("*.md"):
        os.utime(path, (indexed_at - 60, indexed_at - 60))
    (state / "index_meta.json").write_text(
        json.dumps({"last_indexed_at": datetime.fromtimestamp(indexed_at, tz=timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    agent = vault / "Contexts" / "ledger-api" / ".claude" / "agents" / "x.md"
    agent.write_text(agent.read_text(encoding="utf-8") + "\nEdited after the reindex.\n", encoding="utf-8")
    monkeypatch.setattr(webhook, "_staleness_cache", {"checked_at": 0.0, "payload": {}})

    assert webhook._staleness_probe(vault_root=vault, state_dir=state)["stale"] is False

    note = vault / "Services" / "Ledger Service.md"
    note.write_text(note.read_text(encoding="utf-8") + "\nEdited after the reindex.\n", encoding="utf-8")
    monkeypatch.setattr(webhook, "_staleness_cache", {"checked_at": 0.0, "payload": {}})

    assert webhook._staleness_probe(vault_root=vault, state_dir=state)["stale"] is True
