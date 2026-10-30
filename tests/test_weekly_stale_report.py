"""Canary battery for scripts/weekly-stale-report.py (roadmap T-11.01).

The weekly report is a gate: a scheduled job tells four teams which runbooks
they have to revalidate. A gate that has never been seen failing is worth
nothing, so this suite pins both halves of its behaviour:

MUST detect
    - a runbook past its type's threshold, with the owner inherited from its
      service hub;
    - a document whose owner chain resolves to nothing (``unassigned``);
    - frontmatter that no parser can read — otherwise the document is
      invisible to the index *and* to this report, forever;
    - a ``last_updated`` that is present and unreadable, which is the same
      hole one layer down: not overdue, not undated, not listed;
    - a non-zero exit under ``--fail-on-stale``, including when the only
      problem is a document whose age cannot be computed;
    - a ``--owner`` or ``--type`` that matches nothing in the vault, which is
      a wrong filter and not a clean vault.

MUST NOT touch
    - a fresh document, and one sitting exactly on the threshold (the rule is
      strictly greater than);
    - a document with no ``last_updated`` at all ("no data" is not "stale");
    - anything in ``.trash``, in a plugin dot-folder, or in the memory
      export folder;
    - Obsidian templates whose frontmatter still holds ``{{date}}``
      placeholders — stubs, not knowledge.

The report never reads the real ``vault/``: every case below is built in a
temporary directory.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from knowledge import review

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "weekly-stale-report.py"
_TODAY = date(2026, 9, 5)


def _load_script():
    """Import the hyphenated CLI script as a module.

    Registered in ``sys.modules`` before execution because ``@dataclass``
    resolves its own annotations through ``sys.modules[cls.__module__]``.
    """
    spec = importlib.util.spec_from_file_location("weekly_stale_report", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


report = _load_script()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _ago(days: int) -> str:
    return (_TODAY - timedelta(days=days)).isoformat()


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """A vault carrying one instance of every case the report must classify."""
    root = tmp_path / "vault"

    # Service hubs — the only documents that declare an owner.
    _write(
        root / "Services" / "Middleware.md",
        f"---\ntitle: Middleware\ntype: service\nservice: middleware\n"
        f"owner: dev-php\nlast_updated: {_ago(5)}\n---\n\n# Middleware\n",
    )
    _write(
        root / "Services" / "Metadata.md",
        f"---\ntitle: Metadata\ntype: service\nservice: metadata\n"
        f"owner: dev-async\nlast_updated: {_ago(5)}\n---\n\n# Metadata\n",
    )

    # MUST detect: overdue runbooks, owner inherited through `service`.
    _write(
        root / "Runbooks" / "Overdue Middleware.md",
        f"---\ntitle: Overdue Middleware\ntype: runbook\nservice: middleware\n"
        f"last_updated: {_ago(200)}\n---\n\n# Overdue Middleware\n",
    )
    _write(
        root / "Runbooks" / "Overdue Metadata.md",
        f"---\ntitle: Overdue Metadata\ntype: runbook\nservice: metadata\n"
        f"last_updated: {_ago(120)}\n---\n\n# Overdue Metadata\n",
    )
    # MUST detect: overdue with an owner chain that resolves to nothing.
    _write(
        root / "Runbooks" / "Orphan.md",
        f"---\ntitle: Orphan\ntype: runbook\nservice: ghost-service\n"
        f"last_updated: {_ago(150)}\n---\n\n# Orphan\n",
    )
    # MUST detect: unreadable frontmatter (not a template placeholder).
    _write(
        root / "Runbooks" / "Broken Frontmatter.md",
        "---\ntitle: Broken\ntype: runbook\ntags: [unclosed\n---\n\n# Broken\n",
    )

    # MUST detect: a `last_updated` that is there and unreadable. Two shapes,
    # because the vault holds both: free text, and a template placeholder that
    # was never rendered (`Runbooks/Runbook Template.md` in the real vault).
    _write(
        root / "Runbooks" / "Unreadable Date.md",
        "---\ntitle: Unreadable Date\ntype: runbook\nservice: middleware\n"
        "last_updated: not-a-date\n---\n\n# Unreadable Date\n",
    )
    _write(
        root / "Runbooks" / "Placeholder Date.md",
        "---\ntitle: Placeholder Date\ntype: runbook\nservice: metadata\n"
        "last_updated: YYYY-MM-DD\n---\n\n# Placeholder Date\n",
    )

    # MUST NOT touch: fresh, exactly at the limit, and undated.
    _write(
        root / "Runbooks" / "Fresh.md",
        f"---\ntitle: Fresh\ntype: runbook\nservice: middleware\n"
        f"last_updated: {_ago(3)}\n---\n\n# Fresh\n",
    )
    _write(
        root / "Runbooks" / "Exactly At Limit.md",
        f"---\ntitle: Exactly At Limit\ntype: runbook\nservice: middleware\n"
        f"last_updated: {_ago(90)}\n---\n\n# Exactly At Limit\n",
    )
    _write(
        root / "Runbooks" / "Undated.md",
        "---\ntitle: Undated\ntype: runbook\nservice: middleware\n---\n\n# Undated\n",
    )
    # MUST NOT touch: an architecture doc old enough to trip the runbook
    # threshold but well inside its own 365-day limit.
    _write(
        root / "Architecture" / "Platform.md",
        f"---\ntitle: Platform\ntype: architecture\nservice: middleware\n"
        f"last_updated: {_ago(200)}\n---\n\n# Platform\n",
    )
    # MUST NOT touch: trash, plugin folders, memory exports, templates.
    _write(
        root / ".trash" / "Deleted.md",
        f"---\ntitle: Deleted\ntype: runbook\nservice: middleware\n"
        f"last_updated: {_ago(400)}\n---\n\n# Deleted\n",
    )
    _write(
        root / "Runbooks" / "Templates" / ".space" / "templates" / "Stub.md",
        f"---\ntitle: Stub\ntype: runbook\nservice: middleware\n"
        f"last_updated: {_ago(400)}\n---\n\n# Stub\n",
    )
    _write(
        root / "90 - Memory" / "memory" / "memory.md",
        f"---\ntitle: A memory\ntype: memory\nservice: middleware\n"
        f"last_updated: {_ago(400)}\n---\n\n# A memory\n",
    )
    _write(
        root / "Runbooks" / "Templates" / "Auth Issue Template.md",
        "---\ntype: runbook\nservice: middleware\ncreated: {{date}}\n"
        "last_updated: {{date}}\n---\n\n# RB-XXX\n",
    )
    return root


# ---------------------------------------------------------------------------
# Scan classification
# ---------------------------------------------------------------------------


def test_scan_detects_exactly_the_overdue_documents(vault: Path) -> None:
    result = report.scan_vault(vault, today=_TODAY)

    assert [d["file_path"] for d in result.stale] == [
        "Runbooks/Overdue Middleware.md",  # 200d
        "Runbooks/Orphan.md",  # 150d
        "Runbooks/Overdue Metadata.md",  # 120d
    ]


def test_scan_inherits_the_owner_from_the_service_hub(vault: Path) -> None:
    result = report.scan_vault(vault, today=_TODAY)
    by_path = {d["file_path"]: d for d in result.stale}

    assert by_path["Runbooks/Overdue Middleware.md"]["owner"] == "dev-php"
    assert by_path["Runbooks/Overdue Middleware.md"]["owner_source"] == (
        review.OWNER_FROM_SERVICE_HUB
    )
    assert by_path["Runbooks/Overdue Metadata.md"]["owner"] == "dev-async"


def test_scan_flags_an_unresolvable_owner_instead_of_guessing(vault: Path) -> None:
    result = report.scan_vault(vault, today=_TODAY)
    orphan = next(d for d in result.stale if d["file_path"] == "Runbooks/Orphan.md")

    assert orphan["owner"] == review.UNASSIGNED
    assert orphan["owner_source"] == review.OWNER_UNRESOLVED


def test_scan_surfaces_unreadable_frontmatter(vault: Path) -> None:
    result = report.scan_vault(vault, today=_TODAY)

    assert [item["file_path"] for item in result.unparseable] == [
        "Runbooks/Broken Frontmatter.md"
    ]
    assert result.unparseable[0]["error"]


def test_scan_surfaces_a_last_updated_nobody_can_read(vault: Path) -> None:
    """The hole: present-but-unreadable dates were invisible on every count.

    Not overdue (no age to compute), not ``without_last_updated`` (the field
    is there), not ``unparseable`` (the YAML parsed fine). Three counters and
    the document appeared in none of them.
    """
    result = report.scan_vault(vault, today=_TODAY)
    by_path = {item["file_path"]: item for item in result.invalid_dates}

    assert sorted(by_path) == ["Runbooks/Placeholder Date.md", "Runbooks/Unreadable Date.md"]
    assert by_path["Runbooks/Unreadable Date.md"]["last_updated"] == "not-a-date"
    assert by_path["Runbooks/Placeholder Date.md"]["last_updated"] == "YYYY-MM-DD"
    # Ownership still resolves, so the report can name who has to repair it.
    assert by_path["Runbooks/Unreadable Date.md"]["owner"] == "dev-php"
    assert by_path["Runbooks/Placeholder Date.md"]["owner"] == "dev-async"
    # And it is never smuggled into the overdue list, which would invent an age.
    assert "Runbooks/Unreadable Date.md" not in {d["file_path"] for d in result.stale}


def test_scan_keeps_undated_apart_from_unreadable(vault: Path) -> None:
    """Absent is "no data"; unreadable is "data I could not read"."""
    result = report.scan_vault(vault, today=_TODAY)

    assert result.undated == 1  # only Runbooks/Undated.md
    assert len(result.invalid_dates) == 2
    assert "Runbooks/Undated.md" not in {i["file_path"] for i in result.invalid_dates}


def test_scan_counts_templates_apart_from_real_failures(vault: Path) -> None:
    result = report.scan_vault(vault, today=_TODAY)

    assert result.templates == ["Runbooks/Templates/Auth Issue Template.md"]


def test_scan_leaves_fresh_at_limit_and_undated_documents_alone(vault: Path) -> None:
    result = report.scan_vault(vault, today=_TODAY)
    paths = {d["file_path"] for d in result.stale}

    assert "Runbooks/Fresh.md" not in paths
    assert "Runbooks/Exactly At Limit.md" not in paths
    assert "Runbooks/Undated.md" not in paths
    assert result.undated == 1


def test_scan_respects_the_threshold_of_each_type(vault: Path) -> None:
    paths = {d["file_path"] for d in report.scan_vault(vault, today=_TODAY).stale}

    # 200 days old: overdue as a runbook (90), fine as architecture (365).
    assert "Architecture/Platform.md" not in paths
    assert "Runbooks/Overdue Middleware.md" in paths


def test_scan_never_reports_trash_plugin_or_memory_documents(vault: Path) -> None:
    result = report.scan_vault(vault, today=_TODAY)
    paths = {d["file_path"] for d in result.stale}

    assert not any(p.startswith(".trash/") for p in paths)
    assert not any("/.space/" in p for p in paths)
    assert not any(p.startswith("90 - Memory/") for p in paths)
    assert result.skipped_non_curated == 3


def test_type_filter_narrows_the_report(vault: Path) -> None:
    result = report.scan_vault(vault, today=_TODAY, doc_type="playbook")

    assert result.stale == []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_text_report_groups_by_owner_with_unassigned_last(vault: Path) -> None:
    payload = report.build_payload(
        report.scan_vault(vault, today=_TODAY), today=_TODAY, vault=vault, owner_filter=None
    )
    text = report.render_text(payload)

    assert [g["owner"] for g in payload["owners"]] == [
        "dev-async",
        "dev-php",
        review.UNASSIGNED,
    ]
    assert "dev-php — 1 documento vencido" in text
    assert "Runbooks/Overdue Middleware.md" in text
    assert text.index("dev-php") < text.index(review.UNASSIGNED)


def test_json_report_is_machine_readable(vault: Path) -> None:
    payload = report.build_payload(
        report.scan_vault(vault, today=_TODAY), today=_TODAY, vault=vault, owner_filter=None
    )
    parsed = json.loads(report.render(payload, "json"))

    assert parsed["generated_on"] == "2026-09-05"
    assert parsed["totals"]["stale"] == 3
    assert parsed["totals"]["unassigned"] == 1
    assert parsed["totals"]["unparseable"] == 1
    assert parsed["thresholds_days"]["runbook"] == 90


def test_markdown_report_renders_one_table_per_owner(vault: Path) -> None:
    payload = report.build_payload(
        report.scan_vault(vault, today=_TODAY), today=_TODAY, vault=vault, owner_filter=None
    )
    markdown = report.render_markdown(payload)

    assert markdown.count("| Antigüedad | Límite |") == 3
    assert "## dev-php (1)" in markdown
    assert "## Frontmatter ilegible" in markdown


def test_every_format_reports_the_unreadable_documents(vault: Path) -> None:
    payload = report.build_payload(
        report.scan_vault(vault, today=_TODAY), today=_TODAY, vault=vault, owner_filter=None
    )

    assert payload["totals"]["invalid_last_updated"] == 2
    # `unreadable` is what --fail-on-stale consults: both ways of being
    # unreadable, counted together.
    assert payload["totals"]["unreadable"] == 3  # 2 bad dates + 1 broken frontmatter

    text = report.render_text(payload)
    assert "last_updated ilegible" in text
    assert "Runbooks/Unreadable Date.md" in text

    markdown = report.render_markdown(payload)
    assert "## last_updated ilegible" in markdown
    assert "Runbooks/Placeholder Date.md" in markdown

    parsed = json.loads(report.render(payload, "json"))
    assert [i["file_path"] for i in parsed["invalid_last_updated"]] == [
        "Runbooks/Placeholder Date.md",
        "Runbooks/Unreadable Date.md",
    ]


def test_owner_filter_shows_only_that_teams_queue(vault: Path) -> None:
    payload = report.build_payload(
        report.scan_vault(vault, today=_TODAY), today=_TODAY, vault=vault, owner_filter="dev-php"
    )

    assert payload["totals"]["stale"] == 1
    assert [g["owner"] for g in payload["owners"]] == ["dev-php"]


def test_empty_report_says_so_instead_of_printing_nothing(tmp_path: Path) -> None:
    clean = tmp_path / "clean-vault"
    _write(
        clean / "Runbooks" / "Fresh.md",
        f"---\ntitle: Fresh\ntype: runbook\nlast_updated: {_ago(1)}\n---\n\n# Fresh\n",
    )
    payload = report.build_payload(
        report.scan_vault(clean, today=_TODAY), today=_TODAY, vault=clean, owner_filter=None
    )

    assert "Sin documentos vencidos" in report.render_text(payload)
    assert "Sin documentos vencidos" in report.render_markdown(payload)


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


def test_cli_exits_zero_and_prints_the_report(vault: Path, capsys: pytest.CaptureFixture) -> None:
    code = report.main(["--vault", str(vault), "--today", "2026-09-05"])

    assert code == 0
    assert "Reporte semanal de documentos vencidos" in capsys.readouterr().out


def test_cli_fails_when_asked_to_enforce_zero_stale(vault: Path) -> None:
    code = report.main(["--vault", str(vault), "--today", "2026-09-05", "--fail-on-stale"])

    assert code == 1


def test_cli_passes_when_enforcing_zero_on_a_clean_vault(tmp_path: Path) -> None:
    clean = tmp_path / "clean-vault"
    _write(
        clean / "Runbooks" / "Fresh.md",
        f"---\ntitle: Fresh\ntype: runbook\nlast_updated: {_ago(1)}\n---\n\n# Fresh\n",
    )

    assert report.main(["--vault", str(clean), "--today", "2026-09-05", "--fail-on-stale"]) == 0


def test_cli_fails_when_the_only_problem_is_an_unreadable_document(
    tmp_path: Path,
) -> None:
    """A gate that cannot read a document has not proven it is fresh.

    Nothing here is overdue; one document's age simply cannot be computed.
    Exiting 0 would report a clean vault on the strength of a file the gate
    could not look at.
    """
    murky = tmp_path / "murky-vault"
    _write(
        murky / "Runbooks" / "Fresh.md",
        f"---\ntitle: Fresh\ntype: runbook\nlast_updated: {_ago(1)}\n---\n\n# Fresh\n",
    )
    _write(
        murky / "Runbooks" / "Unreadable.md",
        "---\ntitle: Unreadable\ntype: runbook\nlast_updated: YYYY-MM-DD\n---\n\n# X\n",
    )

    payload = report.build_payload(
        report.scan_vault(murky, today=_TODAY), today=_TODAY, vault=murky, owner_filter=None
    )
    assert payload["totals"]["stale"] == 0

    code = report.main(["--vault", str(murky), "--today", "2026-09-05", "--fail-on-stale"])

    assert code == 1


def test_cli_refuses_to_pass_a_scan_that_read_nothing(tmp_path: Path) -> None:
    """`OK: 0` is not a pass. An empty scan is a wrong path, not a clean vault."""
    empty = tmp_path / "empty-vault"
    empty.mkdir()

    assert report.main(["--vault", str(empty), "--today", "2026-09-05", "--fail-on-stale"]) == 2
    # Without the flag it is just an empty report, not an error.
    assert report.main(["--vault", str(empty), "--today", "2026-09-05"]) == 0


def test_cli_rejects_an_owner_that_matches_nobody(
    vault: Path, capsys: pytest.CaptureFixture
) -> None:
    """A misspelled owner used to paint `--fail-on-stale` green.

    `totals.stale` respects the filter, so `--owner nadie` produced an empty
    queue and exit 0 over a vault with three overdue runbooks.
    """
    code = report.main(
        ["--vault", str(vault), "--today", "2026-09-05", "--owner", "dev-phpp", "--fail-on-stale"]
    )
    err = capsys.readouterr().err

    assert code == 2
    assert "--owner 'dev-phpp'" in err
    # The message has to name what would have been accepted, or the operator
    # cannot tell a typo from an owner with nothing overdue.
    assert "dev-php" in err
    assert review.UNASSIGNED in err


def test_cli_accepts_an_owner_with_nothing_overdue(tmp_path: Path) -> None:
    """The guard must not reject correct input.

    `dev-php` owns a hub and has no overdue document here: an empty queue for
    a real owner is a genuine pass, and must stay one.
    """
    clean = tmp_path / "clean-vault"
    _write(
        clean / "Services" / "Middleware.md",
        f"---\ntitle: Middleware\ntype: service\nservice: middleware\n"
        f"owner: dev-php\nlast_updated: {_ago(2)}\n---\n\n# Middleware\n",
    )
    _write(
        clean / "Runbooks" / "Fresh.md",
        f"---\ntitle: Fresh\ntype: runbook\nservice: middleware\n"
        f"last_updated: {_ago(1)}\n---\n\n# Fresh\n",
    )

    code = report.main(
        ["--vault", str(clean), "--today", "2026-09-05", "--owner", "dev-php", "--fail-on-stale"]
    )

    assert code == 0


def test_cli_accepts_the_unassigned_bucket_as_an_owner(vault: Path) -> None:
    """`unassigned` is a documented bucket, never a typo."""
    code = report.main(
        ["--vault", str(vault), "--today", "2026-09-05", "--owner", review.UNASSIGNED]
    )

    assert code == 0


def test_cli_rejects_a_type_that_does_not_exist_in_the_vault(
    vault: Path, capsys: pytest.CaptureFixture
) -> None:
    """Same hole as `--owner`, one column over."""
    code = report.main(
        ["--vault", str(vault), "--today", "2026-09-05", "--type", "runbok", "--fail-on-stale"]
    )
    err = capsys.readouterr().err

    assert code == 2
    assert "--type 'runbok'" in err
    assert "runbook" in err


def test_cli_writes_the_report_to_a_file(vault: Path, tmp_path: Path) -> None:
    out = tmp_path / "artifacts" / "stale-report.md"

    code = report.main(
        ["--vault", str(vault), "--today", "2026-09-05", "--format", "markdown", "--output", str(out)]
    )

    assert code == 0
    assert "# Documentos vencidos" in out.read_text(encoding="utf-8")


def test_cli_rejects_a_missing_vault(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = report.main(["--vault", str(tmp_path / "nope")])

    assert code == 2
    assert "no existe" in capsys.readouterr().err


def test_cli_rejects_a_malformed_reference_date(vault: Path, capsys: pytest.CaptureFixture) -> None:
    code = report.main(["--vault", str(vault), "--today", "last-tuesday"])

    assert code == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_cli_report_is_reproducible_for_a_fixed_date(vault: Path) -> None:
    """A scheduled job must not produce a different report on a rerun."""
    first = report.render(
        report.build_payload(
            report.scan_vault(vault, today=_TODAY), today=_TODAY, vault=vault, owner_filter=None
        ),
        "json",
    )
    second = report.render(
        report.build_payload(
            report.scan_vault(vault, today=_TODAY), today=_TODAY, vault=vault, owner_filter=None
        ),
        "json",
    )

    assert first == second
