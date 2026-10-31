"""``doctor --shape``: the size distributions of a vault's index, as numbers and nothing else.

The output calibrates the synthetic vault (``tests/synthetic_vault.Shape.from_export``)
and may be pasted into an issue, so the guard is a privacy one: no path, title,
tag, heading, term, vault name or text of the vault may appear in it. Every test
runs the real command line in a subprocess against a copy of the fixture vault
under ``tmp_path``; nothing is patched.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from synthetic_vault import Shape

from knowledge import cli
from knowledge.vault_lint import DOCUMENT_TYPES

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures" / "vault"

#: Strings that exist only in the document this module adds to its copy of the vault.
HIDDEN_TYPE = "zzprivatetype"
HIDDEN_TITLE = "Zzprivate Customer Roadmap"
HIDDEN_TAG = "zzprivate-tag"
HIDDEN_HEADING = "Zzprivate Heading Section"
HIDDEN_BODY_WORD = "zzprivatebodyword"
HIDDEN_SERVICE = "zzprivate-service"
HIDDEN_QUERY_WORD = "zzprivatequery"
HIDDEN_FILE = "Zzprivate Folder/Zzprivate Customer Roadmap.md"

SCHEMA_KEYS = {
    "shape_version",
    "docs",
    "body_chars",
    "chunks_per_doc",
    "sections_per_doc",
    "links_per_doc",
    "type_counts",
    "query_term_df",
}
DISTRIBUTION_KEYS = {"p10", "p50", "p90", "p99", "max"}
SELECTIVITY_KEYS = {"p50", "p90", "p99"}
ALLOWED_TYPE_KEYS = set(DOCUMENT_TYPES) | {cli.SHAPE_OTHER_TYPE}

HIDDEN_DOCUMENT = f"""---
type: {HIDDEN_TYPE}
title: {HIDDEN_TITLE}
service: {HIDDEN_SERVICE}
tags: [{HIDDEN_TAG}]
---
# {HIDDEN_TITLE}

See [[Zzprivate Linked Name]] and [[Another Zzprivate Target|alias]].

## {HIDDEN_HEADING}

{HIDDEN_BODY_WORD} alpha beta gamma.

## Second Zzprivate Heading

more {HIDDEN_BODY_WORD} text.
"""


class Sandbox:
    """A copy of the fixture vault, its own index directory and HOME, and a way to run the CLI on them."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.vault = root / "vault-copy"
        self.index = root / "index-dir"
        self.home = root / "home"
        shutil.copytree(FIXTURE_VAULT, self.vault)
        (self.vault / "Zzprivate Folder").mkdir()
        (self.vault / HIDDEN_FILE).write_text(HIDDEN_DOCUMENT, encoding="utf-8")
        self.home.mkdir()
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "KNOWLEDGE_HOME": str(root / "knowledge-home"),
            "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
            "VAULT_PATH": str(self.vault),
            "KNOWLEDGE_INDEX_DIR": str(self.index),
            "KNOWLEDGE_VAULTS": "",
            "CHROMA_PERSIST_DIR": "",
            "KNOWLEDGE_MEMORY_URL": "",
        }

    def run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "knowledge", *argv],
            env=self.env,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def build_index(self) -> None:
        built = self.run("migrate-index", "--build")
        assert built.returncode == 0, built.stderr

    def record_searches(self, *queries: str) -> None:
        """What a running server leaves in ``usage_stats.json`` after searches were followed by reads."""
        events = [
            {"query": text, "tool": "search", "file_path": "x.md", "rank": 1, "at": "2026-01-01T00:00:00+00:00"}
            for text in queries
        ]
        (self.index / "usage_stats.json").write_text(json.dumps({"query_opens": events}), encoding="utf-8")

    def tree(self) -> list[tuple[str, int, int]]:
        """Every file under the sandbox with its mtime and size (names included: a new file shows up)."""
        return sorted(
            (str(p.relative_to(self.root)), p.stat().st_mtime_ns, p.stat().st_size)
            for p in self.root.rglob("*")
            if p.is_file()
        )

    def index_strings(self) -> set[str]:
        """Every string the index holds that names or quotes the vault: paths, titles, tags, sections, text words."""
        conn = sqlite3.connect(f"file:{self.index / 'index.sqlite'}?mode=ro", uri=True)
        try:
            found: set[str] = set()
            for query in (
                "SELECT file_path, doc_id, title, type, service, status, owner FROM docs",
                "SELECT tag FROM doc_tags",
                "SELECT title, section FROM chunks",
            ):
                for row in conn.execute(query):
                    found.update(str(value) for value in row if value)
            for (text,) in conn.execute("SELECT text FROM chunks"):
                found.update(word for word in str(text).split() if len(word) >= 5)
            return found
        finally:
            conn.close()


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    box = Sandbox(tmp_path)
    box.build_index()
    box.record_searches(
        f"{HIDDEN_QUERY_WORD} {HIDDEN_BODY_WORD}", "alpha beta", f"{HIDDEN_TITLE} gamma", "runbook rollback"
    )
    return box


def _shape(box: Sandbox) -> tuple[dict[str, Any], str]:
    result = box.run("doctor", "--shape")
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return json.loads(result.stdout), result.stdout


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _leaves(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return [leaf for value in node.values() for leaf in _leaves(value)]
    return [node]


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {key for value in node.values() for key in _keys(value)}
    return set()


def test_the_shape_has_the_documented_schema_and_real_numbers(sandbox: Sandbox) -> None:
    report, _ = _shape(sandbox)

    assert set(report) == SCHEMA_KEYS
    assert report["shape_version"] == 1
    assert report["docs"] == 15, "the 14 fixture documents plus the one this module adds"
    for name in ("body_chars", "chunks_per_doc", "sections_per_doc", "links_per_doc"):
        dist = report[name]
        assert set(dist) == DISTRIBUTION_KEYS
        assert all(isinstance(value, int) for value in dist.values())
        assert dist["p10"] <= dist["p50"] <= dist["p90"] <= dist["p99"] <= dist["max"]
    assert report["body_chars"]["max"] >= len(HIDDEN_DOCUMENT) - 150
    assert report["sections_per_doc"]["max"] >= 2
    assert report["links_per_doc"]["max"] == 2
    assert sum(report["type_counts"].values()) == report["docs"]
    assert set(report["query_term_df"]) == SELECTIVITY_KEYS
    assert 0 < report["query_term_df"]["p50"] <= report["query_term_df"]["p90"] <= report["query_term_df"]["p99"] <= 1


def test_the_shape_is_numbers_and_a_closed_vocabulary_of_keys(sandbox: Sandbox) -> None:
    report, _ = _shape(sandbox)

    assert all(leaf is None or isinstance(leaf, int | float) for leaf in _leaves(report))
    assert _keys(report) <= SCHEMA_KEYS | DISTRIBUTION_KEYS | SELECTIVITY_KEYS | ALLOWED_TYPE_KEYS
    assert report["type_counts"][cli.SHAPE_OTHER_TYPE] >= 1, f"{HIDDEN_TYPE!r} is counted, never named"
    assert HIDDEN_TYPE not in report["type_counts"]


def test_the_shape_leaks_nothing_the_vault_or_the_searches_say(sandbox: Sandbox) -> None:
    _, out = _shape(sandbox)
    lowered = out.lower()
    allowed_words = {key.lower() for key in SCHEMA_KEYS | DISTRIBUTION_KEYS | SELECTIVITY_KEYS | ALLOWED_TYPE_KEYS}

    for hidden in (
        HIDDEN_TYPE,
        HIDDEN_TITLE,
        HIDDEN_TAG,
        HIDDEN_HEADING,
        HIDDEN_BODY_WORD,
        HIDDEN_SERVICE,
        HIDDEN_QUERY_WORD,
        "Zzprivate",
        HIDDEN_FILE,
        sandbox.vault.name,
        str(sandbox.root),
        str(sandbox.index),
        "vault-copy",
        "index-dir",
        ".md",
        "usage_stats",
    ):
        assert hidden.lower() not in lowered, hidden
    # Everything the index itself holds (paths, titles, tags, sections, words of the text) is absent too: no
    # word of it is a word of the output, except the words that are schema keys or contract document types.
    vocabulary = _words(" ".join(allowed_words))
    out_words = {word for word in _words(lowered) - vocabulary if not word.isdigit()}
    assert out_words == set(), f"words in the output that no schema key accounts for: {sorted(out_words)}"
    for text in sandbox.index_strings():
        assert not (_words(text) & out_words), text
    assert not [line for line in out.splitlines() if "/" in line or "\\" in line]


def test_the_shape_writes_nothing(sandbox: Sandbox) -> None:
    before = sandbox.tree()

    sandbox.run("doctor", "--shape")
    sandbox.run("doctor", "--shape")

    assert sandbox.tree() == before, "no file created, rewritten or touched, the index and its sidecars included"


def test_the_shape_is_deterministic(sandbox: Sandbox) -> None:
    assert sandbox.run("doctor", "--shape").stdout == sandbox.run("doctor", "--shape").stdout


def test_the_selectivity_is_null_without_recorded_searches(sandbox: Sandbox) -> None:
    (sandbox.index / "usage_stats.json").unlink()

    report, _ = _shape(sandbox)

    assert report["query_term_df"] is None


def test_the_synthetic_vault_generator_consumes_the_export(sandbox: Sandbox) -> None:
    report, _ = _shape(sandbox)

    shape = Shape.from_export(report)

    assert shape.body_median_chars == report["body_chars"]["p50"]
    assert dict(shape.type_weights) == {name: float(n) for name, n in report["type_counts"].items()}
    assert shape.query_selectivity == report["query_term_df"]["p50"]


def test_shape_refuses_to_combine_with_the_other_doctor_modes(sandbox: Sandbox) -> None:
    for flag in ("--brief", "--client"):
        result = sandbox.run("doctor", "--shape", flag)
        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr.startswith("E_ARGUMENT_CONFLICT:")


def test_shape_without_an_index_is_a_configuration_error_that_names_no_path(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)

    result = box.run("doctor", "--shape")

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("E_INDEX_UNREADABLE:")
    assert str(tmp_path) not in result.stderr
    assert not box.index.exists(), "nothing was created"


def test_doctor_without_shape_is_unchanged(sandbox: Sandbox) -> None:
    plain = sandbox.run("doctor")
    brief = sandbox.run("doctor", "--brief")

    assert plain.returncode == 0 and brief.returncode == 0
    assert set(json.loads(plain.stdout)) & SCHEMA_KEYS == set()
    assert brief.stdout.startswith("knowledge doctor: ok, contract ")
