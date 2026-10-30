"""``migrate-index``: each vault's ``index.sqlite`` prepared, its Chroma sidecars kept, nothing embedded.

Every case works on throwaway directories under ``tmp_path``: a fake Chroma
directory (``chroma.sqlite3``, a segment directory, ``usage_stats.json`` and a
``link_graph.json`` with edges and learned weights), and the fixture vaults of
``tests/fixtures/vaults``. No case reads a real index or the real vault.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from knowledge import cli, errors, registry, server, settings, stores
from knowledge.embeddings import BagOfWordsEmbeddings
from knowledge.registry import (
    MIGRATE_CREATED,
    MIGRATE_MIGRATED,
    MIGRATE_UP_TO_DATE,
    VaultRegistry,
    build_runtime,
    migrate_index,
)
from knowledge.stores import INDEX_FILE, SCHEMA_VERSION, SQLiteStore, read_schema_version

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures" / "vault"
FIXTURE_VAULTS = REPO_ROOT / "tests" / "fixtures" / "vaults"

#: A learned weight on an edge the fixture vault really has.
PAIR = "Services/Lookup Service.md||Services/Middleware Service.md"


def _chroma_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "chroma.sqlite3").write_bytes(b"SQLite format 3\x00 not really chroma")
    segment = path / "0f6d1c1e-0000-4000-8000-000000000000"
    segment.mkdir()
    (segment / "data_level0.bin").write_bytes(b"\x00" * 16)
    (segment / "header.bin").write_bytes(b"\x00")
    (path / "usage_stats.json").write_text(json.dumps({"uses": {"Services/Lookup Service.md": 3}}))
    (path / "link_graph.json").write_text(
        json.dumps({"outgoing": {"a.md": ["b.md"]}, "incoming": {"b.md": ["a.md"]}, "weights": {PAIR: 1.5}})
    )
    return path


def _spec(tmp_path: Path, *, chroma: Path | None, vault: Path = FIXTURE_VAULT) -> settings.VaultSpec:
    return dataclasses.replace(
        settings.current().default_vault(), path=vault, index_dir=tmp_path / "index", chroma_dir=chroma
    )


def _leftovers(index_dir: Path) -> list[str]:
    return sorted(p.name for p in index_dir.iterdir() if p.name.endswith((".tmp", "-wal", "-shm")))


def test_without_a_chroma_index_an_empty_index_is_created(tmp_path: Path) -> None:
    spec = _spec(tmp_path, chroma=tmp_path / "no-chroma-here")

    migration = migrate_index(spec)

    assert migration.to_json() == {"vault": spec.slug, "outcome": MIGRATE_CREATED, "index_version": 1, "from": None}
    assert read_schema_version(spec.index_dir / INDEX_FILE) == SCHEMA_VERSION
    assert _leftovers(spec.index_dir) == []
    store = SQLiteStore.open(spec.index_dir)
    assert store.count() == 0
    assert store.get_meta()["vault_slug"] == spec.slug
    assert "migrated_from" not in store.get_meta()
    store.close()


def test_a_chroma_index_gives_its_usage_and_learned_weights_but_not_its_edges(tmp_path: Path) -> None:
    chroma = _chroma_dir(tmp_path / "chroma")
    spec = _spec(tmp_path, chroma=chroma)

    migration = migrate_index(spec)

    assert (migration.outcome, migration.source) == (MIGRATE_MIGRATED, "chroma")
    assert (spec.index_dir / "usage_stats.json").read_bytes() == (chroma / "usage_stats.json").read_bytes()
    assert json.loads((spec.index_dir / "link_graph.json").read_text()) == {"weights": {PAIR: 1.5}}
    store = SQLiteStore.open(spec.index_dir)
    assert store.get_meta()["migrated_from"] == "chroma"
    store.close()
    assert (chroma / "chroma.sqlite3").is_file(), "the Chroma index is never deleted without --prune-chroma"


def test_a_chroma_directory_that_is_the_index_directory_keeps_its_sidecars_in_place(tmp_path: Path) -> None:
    chroma = _chroma_dir(tmp_path / "index")
    before = (chroma / "link_graph.json").read_bytes()
    spec = _spec(tmp_path, chroma=chroma)

    assert migrate_index(spec).outcome == MIGRATE_MIGRATED
    assert (chroma / "link_graph.json").read_bytes() == before
    assert (chroma / INDEX_FILE).is_file() and (chroma / "chroma.sqlite3").is_file()


def test_a_second_run_writes_nothing(tmp_path: Path) -> None:
    spec = _spec(tmp_path, chroma=_chroma_dir(tmp_path / "chroma"))
    migrate_index(spec)
    target = spec.index_dir / INDEX_FILE
    stat = target.stat()
    files = sorted(p.name for p in spec.index_dir.iterdir())

    again = migrate_index(spec)

    assert (again.outcome, again.source) == (MIGRATE_UP_TO_DATE, None)
    assert (target.stat().st_mtime_ns, target.stat().st_size) == (stat.st_mtime_ns, stat.st_size)
    assert sorted(p.name for p in spec.index_dir.iterdir()) == files


def test_an_interrupted_run_is_redone_from_scratch(tmp_path: Path) -> None:
    spec = _spec(tmp_path, chroma=_chroma_dir(tmp_path / "chroma"))
    spec.index_dir.mkdir(parents=True)
    (spec.index_dir / f"{INDEX_FILE}.tmp").write_bytes(b"half-written")

    assert migrate_index(spec).outcome == MIGRATE_MIGRATED
    assert _leftovers(spec.index_dir) == []


def test_a_newer_layout_is_refused(tmp_path: Path) -> None:
    import sqlite3

    spec = _spec(tmp_path, chroma=None)
    spec.index_dir.mkdir(parents=True)
    conn = sqlite3.connect(spec.index_dir / INDEX_FILE)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()

    with pytest.raises(errors.KnowledgeConfigError) as exc_info:
        migrate_index(spec)
    assert exc_info.value.code == errors.E_INDEX_VERSION_NEWER


def test_prune_deletes_only_chromas_own_files(tmp_path: Path) -> None:
    chroma = _chroma_dir(tmp_path / "index")
    (chroma / "notes").mkdir()
    spec = _spec(tmp_path, chroma=chroma)

    migration = migrate_index(spec, prune=True, vault_paths=[FIXTURE_VAULT])

    assert migration.pruned == ("chroma.sqlite3", "0f6d1c1e-0000-4000-8000-000000000000")
    assert sorted(p.name for p in chroma.iterdir() if not p.name.startswith(".")) == [
        INDEX_FILE, "link_graph.json", "notes", "usage_stats.json"
    ]


def test_prune_refuses_a_directory_that_holds_a_vault(tmp_path: Path) -> None:
    chroma = _chroma_dir(tmp_path / "chroma")
    vault = chroma / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    spec = _spec(tmp_path, chroma=chroma, vault=vault)

    with pytest.raises(errors.KnowledgeConfigError) as exc_info:
        migrate_index(spec, prune=True, vault_paths=[vault])
    assert exc_info.value.code == errors.E_PRUNE_REFUSED
    assert (chroma / "chroma.sqlite3").is_file()


@pytest.mark.parametrize(
    "content", ['{"weights": {"a||b": 1.', "[1, 2]", '{"weights": [1]}'], ids=["truncated", "a-list", "no-object"]
)
def test_an_unreadable_link_graph_costs_the_weights_not_the_migration(tmp_path: Path, content: str) -> None:
    chroma = _chroma_dir(tmp_path / "chroma")
    (chroma / "link_graph.json").write_text(content, encoding="utf-8")
    spec = _spec(tmp_path, chroma=chroma)

    migration = migrate_index(spec)

    assert migration.outcome == MIGRATE_MIGRATED
    assert (spec.index_dir / "usage_stats.json").is_file()
    assert not (spec.index_dir / "link_graph.json").exists()


def test_a_file_that_is_not_sqlite_is_refused_with_its_code(tmp_path: Path) -> None:
    spec = _spec(tmp_path, chroma=None)
    spec.index_dir.mkdir(parents=True)
    (spec.index_dir / INDEX_FILE).write_bytes(b"not a database at all, " * 8)

    with pytest.raises(errors.KnowledgeConfigError) as exc_info:
        migrate_index(spec)
    assert exc_info.value.code == errors.E_INDEX_UNREADABLE


def test_an_empty_file_is_an_empty_database(tmp_path: Path) -> None:
    spec = _spec(tmp_path, chroma=None)
    spec.index_dir.mkdir(parents=True)
    (spec.index_dir / INDEX_FILE).write_bytes(b"")

    assert migrate_index(spec).outcome == registry.MIGRATE_UPGRADED
    assert read_schema_version(spec.index_dir / INDEX_FILE) == SCHEMA_VERSION


def test_a_store_never_closed_still_carries_its_layout_in_the_header(tmp_path: Path) -> None:
    store = SQLiteStore.open(tmp_path)

    assert read_schema_version(tmp_path / INDEX_FILE) == SCHEMA_VERSION
    store.close()


def test_a_layout_that_only_sits_in_the_wal_is_up_to_date(tmp_path: Path) -> None:
    """A process that died before checkpointing leaves header 0 and the schema in its WAL."""
    work = tmp_path / "work"
    work.mkdir()
    conn = sqlite3.connect(work / INDEX_FILE, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    for statement in stores._SCHEMA:
        conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    spec = _spec(tmp_path, chroma=None)
    spec.index_dir.mkdir(parents=True)
    for name in (INDEX_FILE, f"{INDEX_FILE}-wal"):
        shutil.copyfile(work / name, spec.index_dir / name)
    conn.close()
    assert read_schema_version(spec.index_dir / INDEX_FILE) == 0

    assert migrate_index(spec).outcome == MIGRATE_UP_TO_DATE
    assert read_schema_version(spec.index_dir / INDEX_FILE) == SCHEMA_VERSION


def test_prune_leaves_a_segment_directory_it_cannot_empty(tmp_path: Path) -> None:
    chroma = _chroma_dir(tmp_path / "index")
    segment = chroma / "0f6d1c1e-0000-4000-8000-000000000000"
    (segment / "nested").mkdir()
    spec = _spec(tmp_path, chroma=chroma)

    migration = migrate_index(spec, prune=True, vault_paths=[FIXTURE_VAULT])

    assert migration.pruned == ("chroma.sqlite3",)
    assert sorted(p.name for p in segment.iterdir()) == ["nested"]


def test_learned_weights_survive_the_first_tool_after_migrating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The index fills lazily on the first tool call; the migrated weight 1.5 is still there afterwards."""
    spec = _spec(tmp_path, chroma=_chroma_dir(tmp_path / "chroma"))
    migrate_index(spec)
    rt = build_runtime(spec, embedder=BagOfWordsEmbeddings())
    monkeypatch.setattr(server, "_registry", VaultRegistry.of(rt))

    rows = asyncio.run(server.get_related("Services/Middleware Service.md", limit=10))

    assert rt.indexer.store.count() == 14
    lookup = next(row for row in rows if row["file_path"] == "Services/Lookup Service.md")
    assert lookup["weight"] == 1.5
    saved = json.loads((spec.index_dir / "link_graph.json").read_text())
    assert saved["weights"] == {PAIR: 1.5}
    assert saved["outgoing"], "edges are rebuilt from the vault, not copied"


# --- the command line ----------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No .env, no user registry, a fresh data home holding Chroma directories for alpha and beta."""
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_k: False)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    for key in ("KNOWLEDGE_VAULTS", "VAULT_PATH", "OPENAI_API_KEY", "CHROMA_PERSIST_DIR", "KNOWLEDGE_INDEX_DIR"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("KNOWLEDGE_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("KNOWLEDGE_EMBEDDINGS_PROVIDER", "bag-of-words")
    lines = ["version: 1", "vaults:"]
    for slug in ("alpha", "beta"):
        lines += [f"  - slug: {slug}", f"    path: {FIXTURE_VAULTS / slug}"]
    lines.append("    default: true")
    (tmp_path / "vaults.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _chroma_dir(tmp_path / "data" / "alpha" / "chroma")
    return tmp_path


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_the_command_prints_one_line_per_vault(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["migrate-index", "--vaults", str(cli_env / "vaults.yaml")])

    assert rc == 0
    assert _lines(capsys) == [
        {"vault": "alpha", "outcome": "migrated", "index_version": 1, "from": "chroma"},
        {"vault": "beta", "outcome": "created", "index_version": 1, "from": None},
    ]
    assert (cli_env / "data" / "alpha" / "usage_stats.json").is_file()

    assert cli.main(["migrate-index", "--vaults", str(cli_env / "vaults.yaml"), "--vault", "beta"]) == 0
    assert _lines(capsys) == [{"vault": "beta", "outcome": "up_to_date", "index_version": 1, "from": None}]


def test_build_indexes_each_vault(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["migrate-index", "--vaults", str(cli_env / "vaults.yaml"), "--vault", "beta", "--build"])

    assert rc == 0
    (line,) = _lines(capsys)
    assert line["outcome"] == "created"
    assert line["built"]["files"] == line["built"]["new"] > 0


def test_from_chroma_needs_one_vault(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["migrate-index", "--vaults", str(cli_env / "vaults.yaml"), "--from-chroma", str(cli_env)])

    assert rc == 2
    assert capsys.readouterr().err.startswith(f"{errors.E_ARGUMENT_CONFLICT}: ")


def test_from_chroma_names_the_source_of_one_vault(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    other = _chroma_dir(cli_env / "elsewhere")
    rc = cli.main(
        ["migrate-index", "--vaults", str(cli_env / "vaults.yaml"), "--vault", "beta", "--from-chroma", str(other)]
    )

    assert rc == 0
    assert _lines(capsys) == [{"vault": "beta", "outcome": "migrated", "index_version": 1, "from": "chroma"}]


def test_a_refused_prune_exits_2_with_its_code(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    holder = _chroma_dir(cli_env / "holder")
    shutil.copytree(FIXTURE_VAULTS / "alpha", holder / "alpha")
    (cli_env / "inside.yaml").write_text(
        f"version: 1\nvaults:\n  - slug: alpha\n    path: {holder / 'alpha'}\n", encoding="utf-8"
    )

    rc = cli.main(
        ["migrate-index", "--vaults", str(cli_env / "inside.yaml"), "--from-chroma", str(holder), "--prune-chroma"]
    )

    assert rc == 2
    assert capsys.readouterr().err.startswith(f"{errors.E_PRUNE_REFUSED}: ")
    assert (holder / "chroma.sqlite3").is_file()


def test_an_unreadable_index_exits_2_with_its_code(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    index_dir = cli_env / "data" / "beta"
    index_dir.mkdir(parents=True)
    # The SQLite magic, user_version 0 and a page size of 3: SQLite itself rejects the file.
    (index_dir / INDEX_FILE).write_bytes(b"SQLite format 3\x00" + b"\x00\x03" + b"\x00" * 82 + b"\xff" * 200)

    rc = cli.main(["migrate-index", "--vaults", str(cli_env / "vaults.yaml"), "--vault", "beta"])

    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith(f"{errors.E_INDEX_UNREADABLE}: ")
    assert "Traceback" not in err


def test_registry_exports_the_migration() -> None:
    assert {"migrate_index", "Migration"} <= set(registry.__all__)


# --- two processes on one index directory ----------------------------------------

_INDEX_ONCE = """
import json, sys, time
from pathlib import Path
from knowledge.embeddings import BagOfWordsEmbeddings
from knowledge.indexer import VaultIndexer
from knowledge.stores import SQLiteStore
from knowledge.vault_source import DirectoryVaultSource

class Counting(BagOfWordsEmbeddings):
    embedded = 0

    def embed(self, texts):
        Counting.embedded += len(texts)
        time.sleep(0.05 * len(texts))
        return super().embed(texts)

vault, index_dir = Path(sys.argv[1]), Path(sys.argv[2])
store = SQLiteStore.open(index_dir)
indexer = VaultIndexer(source=DirectoryVaultSource(vault), store=store, embedder=Counting(), state_dir=index_dir)
indexer.index()
store.close()
print(json.dumps({"embedded": Counting.embedded}))
"""

_MIGRATE_ONCE = """
import dataclasses, json, sys
from pathlib import Path
from knowledge import registry, settings

spec = dataclasses.replace(
    settings.current().default_vault(), path=Path(sys.argv[1]), index_dir=Path(sys.argv[2]), chroma_dir=Path(sys.argv[3])
)
print(json.dumps(registry.migrate_index(spec).to_json()))
"""


def _together(script: str, *args: Path) -> list[dict[str, Any]]:
    """Run ``script`` in two processes started together; each prints one JSON line."""
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        results.append(json.loads(out.strip().splitlines()[-1]))
    return results


def test_two_processes_indexing_one_directory_embed_each_chunk_once(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"

    runs = _together(_INDEX_ONCE, FIXTURE_VAULT, index_dir)

    store = SQLiteStore.open(index_dir)
    chunks = store.count()
    store.close()
    assert chunks > 0
    assert sorted(run["embedded"] for run in runs) == [0, chunks]


def test_two_processes_migrating_one_directory_migrate_it_once(tmp_path: Path) -> None:
    chroma = _chroma_dir(tmp_path / "chroma")

    runs = _together(_MIGRATE_ONCE, FIXTURE_VAULT, tmp_path / "index", chroma)

    assert sorted(run["outcome"] for run in runs) == [MIGRATE_MIGRATED, MIGRATE_UP_TO_DATE]
    assert json.loads((tmp_path / "index" / "link_graph.json").read_text()) == {"weights": {PAIR: 1.5}}
