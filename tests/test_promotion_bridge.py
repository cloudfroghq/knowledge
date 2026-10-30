"""Canary battery for scripts/promote-memories.py (roadmap T-11.02).

The promotion bridge is the only path by which a machine-written memory can
become a document a human owns, so the suite pins both halves of its contract:

MUST refuse
    - an output directory that resolves inside the vault, however it was
      spelled (a ``..``, a symlink, or a plain misconfiguration) — the vault is
      read-only for this bridge;
    - a run over a project with zero pinned observations, which is not "no
      candidates" but "nothing to judge";
    - an observation type with no configured target folder, instead of
      dropping the document somewhere plausible;
    - a candidate whose target path already holds a curated document, instead
      of overwriting it;
    - a ``stamp`` for a document that is not in the vault yet, which is what
      would leave a dangling ``knowledge_ref`` behind.

MUST produce
    - one document per candidate, carrying the observation verbatim plus the
      provenance a reviewer needs;
    - a patch whose ``index`` line holds the real git blob id of the content,
      so ``git apply`` accepts it;
    - a manifest whose counters match the scan memory reported;
    - filenames that survive a title full of characters a path cannot hold,
      and two candidates that share a title.

memory is never launched: ``run_memory`` is replaced by a double, so the suite
runs with no binary, no database and no network. The real command is exercised
separately, end to end, against a throwaway memory database.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "promote-memories.py"
_TODAY = "2026-09-05"


def _load_script():
    """Import the hyphenated CLI script as a module.

    Registered in ``sys.modules`` before execution because ``@dataclass``
    resolves its own annotations through ``sys.modules[cls.__module__]``.
    """
    spec = importlib.util.spec_from_file_location("promote_memories", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridge = _load_script()


def _candidate(**overrides) -> dict:
    base = {
        "observation_id": 1,
        "sync_id": "obs-aaaabbbbccccdddd",
        "type": "decision",
        "title": "Coalescing de previews por fileId",
        "content": "Se decide coalescer las peticiones de preview por fileId.",
        "project": "nextcloud",
        "scope": "project",
        "created_at": "2026-09-01 10:00:00",
        "updated_at": "2026-09-01 10:00:00",
    }
    base.update(overrides)
    return base


def _scan(candidates: list[dict], **overrides) -> dict:
    base = {
        "candidates": candidates,
        "types": ["decision", "discovery"],
        "project": "nextcloud",
        "pinned_inspected": len(candidates) + 2,
        "type_excluded": 1,
        "already_promoted": 1,
        "not_syncable": 0,
        "no_project": 0,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def fake_memory(monkeypatch):
    """Replace the memory call with a recorded double.

    Returns a mutable dict the test fills with the envelopes to answer, and
    which collects every command the bridge tried to run — so a test can also
    assert that a refused run never called memory at all.
    """
    state: dict = {"list": None, "stamp": {}, "calls": []}

    def _run(memory_bin, args, timeout=120):  # noqa: ARG001 - signature mirrors the real one
        state["calls"].append(list(args))
        if "list" in args:
            if state["list"] is None:
                raise AssertionError("the test did not prime a `promote list` answer")
            return {"project": "nextcloud", "result": state["list"]}
        if "stamp" in args:
            sync_id = args[args.index("stamp") + 1]
            return {"project": "nextcloud", "result": state["stamp"].get(sync_id, {"stamped": True})}
        raise AssertionError(f"unexpected memory call: {args}")

    monkeypatch.setattr(bridge, "run_memory", _run)
    return state


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway repository holding a throwaway vault in its ``vault/`` subdirectory.

    ``config`` is patched rather than reloaded: the module builds its values at
    import time from the real ``config.json``, and the suite must never point
    at the real vault.
    """
    root = tmp_path / "repo"
    vault = root / "vault"
    vault.mkdir(parents=True)
    (root / ".git").mkdir()
    monkeypatch.setattr(bridge.config, "VAULT_PATH", vault)
    monkeypatch.setattr(
        bridge.config,
        "PROMOTION_TARGET_FOLDERS",
        {"decision": "Architecture/Decisions", "discovery": "Architecture/Discoveries"},
    )
    monkeypatch.setattr(bridge.config, "PROMOTION_DOC_STATUS", "draft")
    monkeypatch.setattr(bridge.config, "PROMOTION_DEFAULT_OWNER", "plat")
    monkeypatch.setattr(bridge.config, "PROMOTION_BRANCH_PREFIX", "feature/memory-promotion")
    monkeypatch.setattr(bridge.config, "PROMOTION_FROM_CONFIG", True)
    return root


def _plan(repo: Path, out: Path, scan: dict, *, types=None, limit=0, dry_run=False, run_id="run1"):
    return bridge.build_plan(
        memory_bin=Path("/nonexistent/memory"),
        project="nextcloud",
        types=types or [],
        limit=limit,
        output_root=out,
        run_id=run_id,
        today=_TODAY,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# The vault is read-only
# ---------------------------------------------------------------------------


def test_output_dir_inside_the_vault_is_refused(repo: Path, fake_memory):
    fake_memory["list"] = _scan([_candidate()])
    inside = bridge.config.VAULT_PATH / "build"
    with pytest.raises(bridge.BridgeError) as exc:
        _plan(repo, inside, fake_memory["list"])
    assert exc.value.code == bridge.EXIT_NOTHING_TO_INSPECT
    # The refusal happens before memory is consulted: a run that must not
    # write must also not spend a call proving it.
    assert fake_memory["calls"] == []


def test_output_dir_reaching_the_vault_through_traversal_is_refused(repo: Path, tmp_path: Path, fake_memory):
    fake_memory["list"] = _scan([_candidate()])
    sneaky = tmp_path / "repo" / "vault" / ".." / "vault" / "out"
    with pytest.raises(bridge.BridgeError) as exc:
        _plan(repo, sneaky, fake_memory["list"])
    assert exc.value.code == bridge.EXIT_NOTHING_TO_INSPECT


def test_a_full_plan_writes_nothing_inside_the_vault(repo: Path, tmp_path: Path, fake_memory):
    fake_memory["list"] = _scan([_candidate(), _candidate(observation_id=2, sync_id="obs-2", type="discovery",
                                                          title="El .part único evita colisiones")])
    before = sorted(p.name for p in bridge.config.VAULT_PATH.rglob("*"))
    _plan(repo, tmp_path / "out", fake_memory["list"])
    after = sorted(p.name for p in bridge.config.VAULT_PATH.rglob("*"))
    assert before == after == []


# ---------------------------------------------------------------------------
# Zero inspected units is never a pass
# ---------------------------------------------------------------------------


def test_zero_pinned_observations_is_an_error_not_an_empty_plan(repo: Path, tmp_path: Path, fake_memory):
    fake_memory["list"] = _scan([], pinned_inspected=0, type_excluded=0, already_promoted=0)
    with pytest.raises(bridge.BridgeError) as exc:
        _plan(repo, tmp_path / "out", fake_memory["list"])
    assert exc.value.code == bridge.EXIT_NOTHING_TO_INSPECT


def test_pinned_but_all_already_promoted_is_a_clean_empty_plan(repo: Path, tmp_path: Path, fake_memory):
    fake_memory["list"] = _scan([], pinned_inspected=4, already_promoted=4, type_excluded=0)
    result = _plan(repo, tmp_path / "out", fake_memory["list"])
    assert result.entries == []
    manifest = json.loads((result.output_dir / "manifest.json").read_text(encoding="utf-8"))
    # An empty plan still says what it looked at, which is the only thing that
    # separates it from a scan that never ran.
    assert manifest["scan"]["pinned_inspected"] == 4
    assert manifest["scan"]["already_promoted"] == 4


# ---------------------------------------------------------------------------
# Rendering and target resolution
# ---------------------------------------------------------------------------


def test_an_unconfigured_type_is_refused_not_guessed(repo: Path, tmp_path: Path, fake_memory, monkeypatch):
    monkeypatch.setattr(bridge.config, "PROMOTION_TARGET_FOLDERS", {"decision": "Architecture/Decisions"})
    fake_memory["list"] = _scan([_candidate(type="discovery")])
    with pytest.raises(bridge.BridgeError) as exc:
        _plan(repo, tmp_path / "out", fake_memory["list"])
    assert exc.value.code == bridge.EXIT_ENVIRONMENT


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Coalescing de previews", "Coalescing de previews"),
        ("Rutas: a/b y c\\d", "Rutas a b y c d"),
        ("Un   título    con   espacios", "Un título con espacios"),
        ("#anchor [wiki] |pipe| ^caret", "anchor wiki pipe caret"),
        ("   ", "obs-fallback"),
        ("", "obs-fallback"),
        ("." * 5, "obs-fallback"),
    ],
)
def test_safe_filename(title, expected):
    assert bridge.safe_filename(title, "obs-fallback") == expected


def test_safe_filename_is_capped_but_never_empty():
    long_title = "a" * 500
    stem = bridge.safe_filename(long_title, "obs-fallback")
    assert 0 < len(stem) <= 80


def test_rendered_document_carries_the_observation_and_its_provenance(repo: Path, tmp_path: Path, fake_memory):
    candidate = _candidate(jira_key="ACME-10336", runbook_id="RB-003", graph_commit="a" * 40, topic_key="incident/preview")
    fake_memory["list"] = _scan([candidate])
    result = _plan(repo, tmp_path / "out", fake_memory["list"])

    assert len(result.entries) == 1
    entry = result.entries[0]
    doc = (result.output_dir / "documents" / entry.knowledge_ref).read_text(encoding="utf-8")

    import frontmatter

    post = frontmatter.loads(doc)
    assert post.metadata["type"] == "decision"
    assert post.metadata["status"] == "draft"
    assert post.metadata["owner"] == "plat"
    assert post.metadata["last_updated"] == _TODAY
    assert post.metadata["promoted_from"] == candidate["sync_id"]
    assert post.metadata["source"] == "memory-promotion"
    assert post.metadata["memory_project"] == "nextcloud"
    assert post.metadata["jira_key"] == "ACME-10336"
    assert post.metadata["runbook_id"] == "RB-003"
    assert post.metadata["graph_commit"] == "a" * 40
    # The body travels verbatim: a promoted document that says less than the
    # memory it came from is worse than no promotion.
    assert candidate["content"] in post.content
    assert candidate["title"] in post.content
    assert "incident/preview" in post.content


def test_optional_pointers_are_omitted_when_absent(repo: Path, tmp_path: Path, fake_memory):
    fake_memory["list"] = _scan([_candidate()])
    result = _plan(repo, tmp_path / "out", fake_memory["list"])
    doc = (result.output_dir / "documents" / result.entries[0].knowledge_ref).read_text(encoding="utf-8")

    import frontmatter

    post = frontmatter.loads(doc)
    for absent in ("jira_key", "runbook_id", "graph_commit"):
        assert absent not in post.metadata


def test_two_candidates_sharing_a_title_both_survive(repo: Path, tmp_path: Path, fake_memory):
    fake_memory["list"] = _scan(
        [
            _candidate(observation_id=1, sync_id="obs-1111"),
            _candidate(observation_id=2, sync_id="obs-2222"),
        ]
    )
    result = _plan(repo, tmp_path / "out", fake_memory["list"])
    refs = [e.knowledge_ref for e in result.entries]
    assert len(refs) == 2
    assert len(set(refs)) == 2, f"one candidate silently overwrote the other: {refs}"
    assert "obs-2222" in refs[1]


# ---------------------------------------------------------------------------
# Collisions with curated documents
# ---------------------------------------------------------------------------


def test_a_candidate_colliding_with_a_curated_document_is_excluded(repo: Path, tmp_path: Path, fake_memory):
    occupied = bridge.config.VAULT_PATH / "Architecture" / "Decisions" / "Coalescing de previews por fileId.md"
    occupied.parent.mkdir(parents=True)
    occupied.write_text("---\ntype: architecture\n---\n\n# Escrito por una persona\n", encoding="utf-8")
    original = occupied.read_text(encoding="utf-8")

    fake_memory["list"] = _scan([_candidate()])
    result = _plan(repo, tmp_path / "out", fake_memory["list"])

    assert result.entries == []
    assert len(result.collisions) == 1
    assert result.collisions[0]["sync_id"] == "obs-aaaabbbbccccdddd"
    # The curated document is untouched, and the collision is in the manifest
    # rather than only on stdout, so the pull request carries it.
    assert occupied.read_text(encoding="utf-8") == original
    manifest = json.loads((result.output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["collisions"]) == 1
    assert "Choques sin resolver" in (result.output_dir / "PR.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The patch
# ---------------------------------------------------------------------------


def test_patch_index_line_holds_the_real_git_blob_id():
    content = "---\ntype: decision\n---\n\n# Hola\n"
    patch = bridge.new_file_patch("Architecture/Decisions/Hola.md", content)
    data = content.encode("utf-8")
    expected = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()  # noqa: S324
    assert f"index 0000000..{expected[:7]}" in patch
    assert patch.startswith("diff --git a/Architecture/")
    assert "--- /dev/null" in patch
    assert "@@ -0,0 +1,5 @@" in patch


def test_patch_paths_are_relative_to_the_vault_repository(tmp_path: Path, monkeypatch):
    # A vault in a subdirectory of its repository keeps that subdirectory in the
    # patch; a vault at the repository root (the usual case) has no prefix.
    root = tmp_path / "vault-repo"
    vault = root / "docs" / "vault"
    vault.mkdir(parents=True)
    (root / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    monkeypatch.setattr(bridge.config, "VAULT_PATH", vault)
    assert bridge.vault_repo_prefix() == "docs/vault"
    monkeypatch.setattr(bridge.config, "VAULT_PATH", root)
    assert bridge.vault_repo_prefix() == ""


def test_a_vault_outside_any_repository_is_refused(tmp_path: Path, monkeypatch):
    vault = tmp_path / "loose-vault"
    vault.mkdir()
    if any((p / ".git").exists() for p in (vault, *vault.parents)):
        pytest.skip("the temporary directory sits inside a git checkout")
    monkeypatch.setattr(bridge.config, "VAULT_PATH", vault)
    with pytest.raises(bridge.BridgeError) as excinfo:
        bridge.vault_repo_prefix()
    assert excinfo.value.code == bridge.EXIT_NOTHING_TO_INSPECT


def test_patch_applies_in_a_real_git_repository(repo: Path, tmp_path: Path, fake_memory):
    fake_memory["list"] = _scan(
        [
            _candidate(),
            _candidate(observation_id=2, sync_id="obs-2222", type="discovery", title="El .part único evita colisiones"),
        ]
    )
    result = _plan(repo, tmp_path / "out", fake_memory["list"])

    # A throwaway repository, never the real one: the patch is proved by
    # applying it, not by reading it.
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=work, check=True)
    patch = (result.output_dir / "promotion.patch").read_text(encoding="utf-8")
    check = subprocess.run(
        ["git", "apply", "--check", "-"], cwd=work, input=patch, text=True, capture_output=True, check=False
    )
    assert check.returncode == 0, check.stderr
    applied = subprocess.run(
        ["git", "apply", "-"], cwd=work, input=patch, text=True, capture_output=True, check=False
    )
    assert applied.returncode == 0, applied.stderr

    for entry in result.entries:
        landed = work / entry.repo_path
        assert landed.is_file(), f"{entry.repo_path} did not land"
        assert entry.repo_path.startswith("vault/Architecture/")
        # The manifest's sha256 describes the bytes that actually landed.
        digest = hashlib.sha256(landed.read_bytes()).hexdigest()
        assert digest == entry.sha256


def test_dry_run_writes_nothing(repo: Path, tmp_path: Path, fake_memory):
    out = tmp_path / "out"
    fake_memory["list"] = _scan([_candidate()])
    result = _plan(repo, out, fake_memory["list"], dry_run=True)
    assert len(result.entries) == 1
    assert result.output_dir is None
    assert not out.exists()


# ---------------------------------------------------------------------------
# stamp
# ---------------------------------------------------------------------------


def _manifest(tmp_path: Path, vault: Path, entries: list[dict]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"project": "nextcloud", "vault_path": str(vault), "entries": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_stamp_refuses_a_document_that_is_not_in_the_vault_yet(repo: Path, tmp_path: Path, fake_memory):
    vault = bridge.config.VAULT_PATH
    manifest = _manifest(
        tmp_path,
        vault,
        [{"sync_id": "obs-1111", "knowledge_ref": "Architecture/Decisions/Sin fusionar.md"}],
    )
    outcome = bridge.run_stamp(memory_bin=Path("/nonexistent/memory"), manifest_path=manifest, dry_run=False)
    assert outcome.stamped == []
    assert outcome.missing == [{"sync_id": "obs-1111", "knowledge_ref": "Architecture/Decisions/Sin fusionar.md"}]
    # Nothing was asked of memory: a stamp for a document nobody merged is the
    # dangling reference this check exists to prevent.
    assert fake_memory["calls"] == []


def test_stamp_marks_a_merged_document(repo: Path, tmp_path: Path, fake_memory):
    vault = bridge.config.VAULT_PATH
    ref = "Architecture/Decisions/Fusionado.md"
    (vault / ref).parent.mkdir(parents=True)
    (vault / ref).write_text("---\ntype: decision\n---\n\n# Fusionado\n", encoding="utf-8")
    manifest = _manifest(tmp_path, vault, [{"sync_id": "obs-1111", "knowledge_ref": ref}])

    outcome = bridge.run_stamp(memory_bin=Path("/nonexistent/memory"), manifest_path=manifest, dry_run=False)
    assert outcome.stamped == ["obs-1111"]
    assert outcome.missing == []
    assert fake_memory["calls"] == [
        ["project", "nextcloud", "promote", "stamp", "obs-1111", "--knowledge-ref", ref, "--json"]
    ]


def test_stamp_reports_an_already_stamped_observation_as_such(repo: Path, tmp_path: Path, fake_memory):
    vault = bridge.config.VAULT_PATH
    ref = "Architecture/Decisions/Fusionado.md"
    (vault / ref).parent.mkdir(parents=True)
    (vault / ref).write_text("---\ntype: decision\n---\n\n# Fusionado\n", encoding="utf-8")
    manifest = _manifest(tmp_path, vault, [{"sync_id": "obs-1111", "knowledge_ref": ref}])
    fake_memory["stamp"]["obs-1111"] = {"stamped": False}

    outcome = bridge.run_stamp(memory_bin=Path("/nonexistent/memory"), manifest_path=manifest, dry_run=False)
    assert outcome.stamped == []
    assert outcome.already == ["obs-1111"]


def test_stamp_dry_run_never_calls_memory(repo: Path, tmp_path: Path, fake_memory):
    vault = bridge.config.VAULT_PATH
    ref = "Architecture/Decisions/Fusionado.md"
    (vault / ref).parent.mkdir(parents=True)
    (vault / ref).write_text("x", encoding="utf-8")
    manifest = _manifest(tmp_path, vault, [{"sync_id": "obs-1111", "knowledge_ref": ref}])

    outcome = bridge.run_stamp(memory_bin=Path("/nonexistent/memory"), manifest_path=manifest, dry_run=True)
    assert outcome.stamped == ["obs-1111"]
    assert fake_memory["calls"] == []


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ('{"project": "nextcloud", "entries": []}', "no entries"),
        ('{"entries": [{"sync_id": "a", "knowledge_ref": "b.md"}]}', "no project"),
        ("not json at all", "unparseable"),
    ],
)
def test_stamp_refuses_an_unusable_manifest(repo: Path, tmp_path: Path, fake_memory, payload, reason):
    path = tmp_path / "manifest.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(bridge.BridgeError) as exc:
        bridge.run_stamp(memory_bin=Path("/nonexistent/memory"), manifest_path=path, dry_run=False)
    assert exc.value.code == bridge.EXIT_NOTHING_TO_INSPECT, reason


def test_stamp_refuses_a_manifest_that_does_not_exist(repo: Path, tmp_path: Path, fake_memory):
    with pytest.raises(bridge.BridgeError) as exc:
        bridge.run_stamp(
            memory_bin=Path("/nonexistent/memory"), manifest_path=tmp_path / "nope.json", dry_run=False
        )
    assert exc.value.code == bridge.EXIT_NOTHING_TO_INSPECT


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Argument validation (T-11.02: --types empty-string regression)
# ---------------------------------------------------------------------------


def test_empty_types_string_is_rejected(repo: Path, tmp_path: Path, fake_memory):
    """Regression test for T-11.02.

    With --types "", argparse delivers an empty string (falsy).
    Before fix: guard `if args.types and not types:` would NOT fire,
               script continues with complete allowlist (wrong behavior).
    After fix:  guard `if args.types is not None and not types:` fires,
                rejects with EXIT_NOTHING_TO_INSPECT (2).
    """
    # Prime memory to avoid hitting the fake early; the guard should
    # intercept before any memory call happens.
    fake_memory["list"] = _scan([])

    # Invoke main() with --types "" (empty string explicitly provided)
    exit_code = bridge.main(
        [
            "plan",
            "--project", "nextcloud",
            "--types", "",
            "--output-dir", str(tmp_path / "out"),
        ]
    )

    # Must reject with code 2, not succeed with code 0
    assert exit_code == bridge.EXIT_NOTHING_TO_INSPECT,         f"Expected EXIT_NOTHING_TO_INSPECT={bridge.EXIT_NOTHING_TO_INSPECT}, got {exit_code}"


# memory plumbing
# ---------------------------------------------------------------------------


def test_run_memory_reports_a_missing_binary_as_an_environment_failure():
    with pytest.raises(bridge.BridgeError) as exc:
        bridge.run_memory(Path("/definitely/not/here/memory"), ["project", "x", "promote", "list", "--json"])
    assert exc.value.code == bridge.EXIT_ENVIRONMENT


def test_run_memory_maps_a_caller_error_code_away_from_the_environment_status(monkeypatch):
    """A mistyped filter must not share an exit status with a broken machine."""

    class _Proc:
        returncode = 1
        stdout = json.dumps({"error": "type is not promotable", "code": "type_not_promotable"})
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(bridge.BridgeError) as exc:
        bridge.run_memory(Path("/bin/true"), ["project", "x", "promote", "list", "--json"])
    assert exc.value.code == bridge.EXIT_NOTHING_TO_INSPECT
    assert "type_not_promotable" in str(exc.value)


def test_run_memory_rejects_output_that_is_not_a_json_object(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = "memory: everything is fine\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(bridge.BridgeError) as exc:
        bridge.run_memory(Path("/bin/true"), ["project", "x", "promote", "list", "--json"])
    assert exc.value.code == bridge.EXIT_ENVIRONMENT


def test_types_none_default_passes_correct_command_to_memory(repo: Path, tmp_path: Path, fake_memory):
    """Regression test for T-11.02: verify --types is omitted when not provided.

    With no --types arg, argparse sets args.types=None.
    Before fix: types would become ["None"] due to str(None).split(','),
                memory receives --types None, rejects with type_not_promotable.
    After fix:  types becomes [] (empty list),
                memory receives command WITHOUT --types, uses its default allowlist.

    This test verifies by inspecting the actual memory command sent.
    """
    # Prime memory with a valid response
    fake_memory["list"] = _scan([])

    # Invoke main() WITHOUT --types (so argparse delivers args.types=None)
    exit_code = bridge.main(
        [
            "plan",
            "--project", "nextcloud",
            "--output-dir", str(tmp_path / "out"),
        ]
    )

    # Must succeed (exit 0), not reject with EXIT_NOTHING_TO_INSPECT
    assert exit_code == 0, f"Expected exit code 0 (success), got {exit_code}"

    # Verify that memory was called WITHOUT --types in the argument list
    assert len(fake_memory["calls"]) == 1, f"Expected 1 memory call, got {len(fake_memory['calls'])}"
    memory_args = fake_memory["calls"][0]

    # Must have "promote list --json" but NOT have "--types"
    assert "promote" in memory_args, f"Expected 'promote' in args: {memory_args}"
    assert "list" in memory_args, f"Expected 'list' in args: {memory_args}"
    assert "--json" in memory_args, f"Expected '--json' in args: {memory_args}"
    assert "--types" not in memory_args, (
        f"ERROR: --types must NOT be in args when not provided. "
        f"Got: {memory_args}. This indicates the bug: types=[] should NOT add --types"
    )
