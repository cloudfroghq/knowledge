"""What ``vault.yaml`` ``index.exclude`` matches is not a note, so it is never on a review queue.

``vault-lint`` already exempts those files from the frontmatter rules of 1.3.
The review queue has to agree: a file the vault excludes is neither overdue,
nor unreadable-dated, nor "without last_updated", in ``get_stale_docs`` and in
the offline report (``scripts/weekly-stale-report.py``), which share
``review.is_curated``. The answer's shape does not change, only which files
enter it.

The vault is built under ``tmp_path`` (never the golden fixture, which declares
no manifest), served through a ``vaults.yaml`` with the deterministic
``bag-of-words`` embeddings.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from knowledge import review, server, settings, vault_lint
from knowledge.registry import VaultRegistry

#: ``get_stale_docs`` measures against the real today, so the dates are relative to it.
TODAY = date.today()
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "weekly-stale-report.py"

MANIFEST = """\
slug: stale
review:
  thresholds_days:
    default: 30
index:
  exclude:
    - "Contexts/**"
    - "**/_Templates/**"
"""


def _ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


def _note(**fields: str) -> str:
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\n\n# body\n"


def _build(root: Path, manifest: str | None = MANIFEST) -> Path:
    files = {
        # Curated, not excluded: all three states must still be reported.
        "Notes/Overdue.md": _note(title="Overdue", type="note", last_updated=_ago(400)),
        "Notes/Unreadable.md": _note(title="Unreadable", type="note", last_updated="not-a-date"),
        "Notes/Undated.md": _note(title="Undated", type="note"),
        "Notes/Fresh.md": _note(title="Fresh", type="note", last_updated=_ago(2)),
        # Excluded: each would land on a queue if it were a note.
        "Contexts/app/Old.md": _note(title="Old", type="note", last_updated=_ago(600)),
        "Contexts/app/Unreadable.md": _note(title="Unreadable", type="note", last_updated="<YYYY-MM-DD>"),
        "Contexts/app/CLAUDE.md": "# app\n\nNo frontmatter at all.\n",
        "Services/_Templates/Template.md": _note(title="Template", type="note", last_updated=_ago(900)),
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if manifest is not None:
        (root / "vault.yaml").write_text(manifest, encoding="utf-8")
    return root


EXCLUDED = {
    "Contexts/app/Old.md",
    "Contexts/app/Unreadable.md",
    "Contexts/app/CLAUDE.md",
    "Services/_Templates/Template.md",
}


@pytest.fixture()
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The vault registered as the default of a fresh registry."""
    vault = _build(tmp_path / "vault")
    vaults_file = tmp_path / "vaults.yaml"
    vaults_file.write_text(
        yaml.safe_dump({"version": 1, "vaults": [{"slug": "stale", "path": str(vault), "default": True}]}),
        encoding="utf-8",
    )
    env = {
        "KNOWLEDGE_HOME": str(tmp_path / "home"),
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
        "KNOWLEDGE_VAULTS": str(vaults_file),
    }
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(server, "_registry", VaultRegistry.from_settings(settings.load_settings(env, project_root=root)))
    return vault


# --- the shared rule ---------------------------------------------------------------


def test_the_curated_rule_honours_a_matcher_and_keeps_its_other_exclusions() -> None:
    excluded = vault_lint.index_exclude_matcher(["Contexts/**", "**/_Templates/**"])

    assert review.is_curated("Notes/A.md", index_exclude=excluded) is True
    assert review.is_curated("Contexts/app/CLAUDE.md", index_exclude=excluded) is False
    assert review.is_curated("Services/_Templates/T.md", index_exclude=excluded) is False
    assert review.is_curated("Contexts/app/CLAUDE.md") is True, "without a matcher nothing new is excluded"
    assert review.is_curated(".trash/A.md", index_exclude=excluded) is False
    assert review.is_curated("90 - Memory/A.md", index_exclude=excluded) is False


def test_the_matcher_is_the_one_the_lint_applies() -> None:
    patterns = ["_templates/**", "**/Pendientes.md", "Archive", "*.md", "a?.md"]
    matcher = vault_lint.index_exclude_matcher(patterns)
    manifest = vault_lint._Manifest(exclude=tuple(vault_lint._glob_regex(p) for p in patterns))

    for rel in ["_templates/a.md", "x/_templates/a.md", "a/b/Pendientes.md", "Archive/x.md", "root.md", "dir/in.md", "ab.md"]:
        assert matcher(rel) is manifest.excludes(rel), rel


def test_a_vault_spec_carries_the_patterns_the_manifest_declares(served: Path) -> None:
    assert server.default_runtime().spec.index_exclude == ("Contexts/**", "**/_Templates/**")


@pytest.mark.parametrize("declared", ["index: {exclude: x}", "index: {exclude: [1]}", "index: [a]", "index: {exclude: [' ']}"])
def test_a_manifest_the_lint_would_refuse_declares_no_patterns_to_the_server(tmp_path: Path, declared: str) -> None:
    assert settings.index_exclude_patterns(yaml.safe_load(declared)) == ()


# --- the tool ------------------------------------------------------------------------


def test_get_stale_docs_leaves_out_what_the_vault_excludes(served: Path) -> None:
    rows = asyncio.run(server.get_stale_docs(missing_last_updated=True, limit=50))

    assert {(r["file_path"], r["reason"]) for r in rows} == {
        ("Notes/Overdue.md", "overdue"),
        ("Notes/Unreadable.md", "last_updated_invalid"),
        ("Notes/Undated.md", "last_updated_missing"),
    }


def test_the_rows_keep_their_shape(served: Path) -> None:
    [overdue] = asyncio.run(server.get_stale_docs(include_unreadable=False))

    assert set(overdue) == {
        "file_path",
        "title",
        "type",
        "service",
        "owner",
        "owner_source",
        "last_updated",
        "last_verified",
        "threshold_days",
        "reason",
        "needs_review",
        "age_days",
    }
    assert overdue["file_path"] == "Notes/Overdue.md" and overdue["age_days"] == 400


def test_without_a_manifest_every_curated_file_stays_on_the_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _build(tmp_path / "plain", manifest=None)
    vaults_file = tmp_path / "vaults.yaml"
    vaults_file.write_text(
        yaml.safe_dump({"version": 1, "vaults": [{"slug": "plain", "path": str(vault), "default": True}]}),
        encoding="utf-8",
    )
    env = {
        "KNOWLEDGE_HOME": str(tmp_path / "home"),
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
        "KNOWLEDGE_VAULTS": str(vaults_file),
    }
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(server, "_registry", VaultRegistry.from_settings(settings.load_settings(env, project_root=root)))

    files = {r["file_path"] for r in asyncio.run(server.get_stale_docs(missing_last_updated=True, limit=50))}

    assert EXCLUDED <= files | {"Contexts/app/CLAUDE.md"}
    assert "Contexts/app/Old.md" in files and "Services/_Templates/Template.md" in files


# --- the report ----------------------------------------------------------------------


def _load_script():
    spec = importlib.util.spec_from_file_location("weekly_stale_report_exclude", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_weekly_report_leaves_out_what_the_vault_excludes(tmp_path: Path) -> None:
    report = _load_script()
    vault = _build(tmp_path / "vault")

    result = report.scan_vault(vault, today=TODAY)

    assert {d["file_path"] for d in result.stale} == {"Notes/Overdue.md"}
    assert {d["file_path"] for d in result.invalid_dates} == {"Notes/Unreadable.md"}
    assert result.undated == 1, "only Notes/Undated.md: Contexts/app/CLAUDE.md is excluded"
    assert result.skipped_non_curated == len(EXCLUDED)
    assert not result.unparseable and not result.templates


def test_the_weekly_report_without_a_manifest_reports_everything_curated(tmp_path: Path) -> None:
    report = _load_script()
    vault = _build(tmp_path / "vault", manifest=None)

    result = report.scan_vault(vault, today=TODAY)

    assert {"Notes/Overdue.md", "Contexts/app/Old.md", "Services/_Templates/Template.md"} <= {
        d["file_path"] for d in result.stale
    }
    assert result.skipped_non_curated == 0


def test_a_manifest_nobody_can_read_is_reported_as_unlooked_at_and_fails_the_gate(tmp_path: Path) -> None:
    report = _load_script()
    vault = tmp_path / "vault"
    for rel in ("Notes/Fresh.md", "Contexts/app/CLAUDE.md"):
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_note(title="x", type="note", last_updated=_ago(1)), encoding="utf-8")
    (vault / "vault.yaml").write_text("slug: [unclosed\n", encoding="utf-8")

    result = report.scan_vault(vault, today=TODAY)

    assert [u["path"] for u in result.unwalkable] == ["vault.yaml"]
    assert "E_MANIFEST_INVALID" in result.unwalkable[0]["error"] or "KnowledgeConfigError" in result.unwalkable[0]["error"]
    assert report.main(["--vault", str(vault), "--today", TODAY.isoformat(), "--fail-on-stale"]) == 1


# --- one rule for the lint, the tool and the report ------------------------------------


def test_lint_tool_and_report_agree_on_which_files_are_not_notes(tmp_path: Path, served: Path) -> None:
    report = _load_script()
    lint_report = vault_lint.lint(served, rules=vault_lint.RULES_1_3)
    flagged_by_lint = {
        e["file"] for e in [*lint_report.errors, *lint_report.warnings] if e["code"].startswith(("E_FRONTMATTER", "W_TYPE"))
    }
    queued_by_tool = {r["file_path"] for r in asyncio.run(server.get_stale_docs(missing_last_updated=True, limit=50))}
    scanned = report.scan_vault(served, today=TODAY)
    queued_by_report = (
        {d["file_path"] for d in scanned.stale}
        | {d["file_path"] for d in scanned.invalid_dates}
    )

    for name, files in (("lint", flagged_by_lint), ("tool", queued_by_tool), ("report", queued_by_report)):
        assert not files & EXCLUDED, f"{name} still reports an excluded file: {sorted(files & EXCLUDED)}"
