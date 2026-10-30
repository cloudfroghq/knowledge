"""The 1.3 vault rules of ``vault-lint`` and its ``--fix``.

Every vault here is either a copy of a tree under ``tests/fixtures/lint/`` or
built in ``tmp_path``; ``--fix`` only ever runs on a temporary copy, and a
test checks the fixtures stay byte for byte as they are.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from knowledge import cli, server, vault_lint
from knowledge.errors import KnowledgeConfigError
from knowledge.vault_lint import RULES_1_2, RULES_1_3, FixOutcome, fix, lint

LINT_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lint"
CLEAN = LINT_FIXTURES / "clean"
LEGACY = LINT_FIXTURES / "legacy"


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _write(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _note(**fields: Any) -> str:
    body = "".join(f"{k}: {v}\n" for k, v in fields.items())
    return f"---\n{body}---\n\n# Note\n"


def _codes(entries: list[dict[str, Any]]) -> Counter[str]:
    return Counter(e["code"] for e in entries)


def _findings(report: Any, code: str) -> list[str]:
    return [e["file"] for e in [*report.errors, *report.warnings] if e["code"] == code]


@pytest.fixture(scope="module", autouse=True)
def _fixtures_stay_untouched() -> Any:
    before = _tree_digest(LINT_FIXTURES)
    yield
    assert _tree_digest(LINT_FIXTURES) == before


# --- rule sets -------------------------------------------------------------------


def test_the_default_rules_are_the_1_2_set_and_the_cli_asks_for_1_3() -> None:
    assert RULES_1_2 == "1.2"
    assert lint(CLEAN).rules == RULES_1_2
    assert vault_lint.LATEST_RULES == RULES_1_3
    parsed = cli._build_parser().parse_args(["vault-lint", "somewhere"])
    assert parsed.rules == RULES_1_3


def test_an_unknown_rule_set_is_refused_with_a_stable_code() -> None:
    with pytest.raises(KnowledgeConfigError) as raised:
        lint(CLEAN, rules="9.9")
    assert raised.value.code == "E_LINT_RULES_UNKNOWN"


def test_the_1_2_rules_ignore_every_1_3_finding(tmp_path: Path) -> None:
    vault = _write(
        tmp_path / "v",
        {
            "vault.yaml": "slug: v\nscopes: not-a-list\n",
            "Note.md": _note(type="mystery", scope="elsewhere"),
            "Runbooks/Bad.md": _note(type="runbook", status="active"),
        },
    )

    old = lint(vault, rules=RULES_1_2)
    new = lint(vault, rules=RULES_1_3)

    assert (old.ok, old.errors, old.warnings) == (True, [], [])
    assert not new.ok


def test_reindex_validation_keeps_the_1_2_rules_in_the_report_the_server_reads() -> None:
    source = Path(server.__file__).read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "lint"
        and getattr(node.func.value, "id", "") == "vault_lint"
    ]
    assert calls, "the server must call vault_lint.lint for reindex.validation"
    assert all(not call.keywords for call in calls), "reindex.validation takes the default (1.2) rule set"


# --- the clean vault ------------------------------------------------------------------


def test_the_clean_vault_passes_every_rule() -> None:
    report = lint(CLEAN, rules=RULES_1_3)
    assert (report.files, report.errors, report.warnings) == (10, [], [])
    assert report.to_json()["rules"] == RULES_1_3


def test_lint_never_writes(tmp_path: Path) -> None:
    vault = tmp_path / "copy"
    shutil.copytree(LEGACY, vault)
    before = _tree_digest(vault)
    lint(vault, rules=RULES_1_3)
    lint(vault, rules=RULES_1_2)
    assert _tree_digest(vault) == before


# --- last_updated -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("front", "message"),
    [
        ("type: note\n", "missing or blank"),
        ("type: note\nlast_updated:\n", "missing or blank"),
        ('type: note\nlast_updated: "  "\n', "missing or blank"),
        ("type: note\nlast_updated: soon\n", "not an ISO date"),
        ("type: note\nlast_updated: '<YYYY-MM-DD>'\n", "not an ISO date"),
        ("type: note\nlast_updated: '2026-13-45'\n", "not an ISO date"),
    ],
)
def test_a_missing_or_unreadable_last_updated_is_an_error(tmp_path: Path, front: str, message: str) -> None:
    vault = _write(tmp_path / "v", {"Note.md": f"---\n{front}---\n\nbody\n"})
    [error] = lint(vault, rules=RULES_1_3).errors
    assert error["code"] == "E_FRONTMATTER_LAST_UPDATED"
    assert error["file"] == "Note.md"
    assert message in error["message"]


@pytest.mark.parametrize("value", ["2026-09-01", "'2026-09-01'", "2026-09-01T10:30:00", "2026-09-01T10:30:00+00:00"])
def test_an_iso_last_updated_passes(tmp_path: Path, value: str) -> None:
    vault = _write(tmp_path / "v", {"Note.md": _note(type="note", last_updated=value)})
    assert lint(vault, rules=RULES_1_3).errors == []


def test_a_note_without_frontmatter_has_no_last_updated(tmp_path: Path) -> None:
    vault = _write(tmp_path / "v", {"README.md": "just text\n"})
    assert _codes(lint(vault, rules=RULES_1_3).errors) == {"E_FRONTMATTER_LAST_UPDATED": 1}


def test_unparseable_frontmatter_is_not_also_a_last_updated_error(tmp_path: Path) -> None:
    vault = _write(tmp_path / "v", {"Bad.md": "---\nkey: [unclosed\n---\nbody\n"})
    assert _codes(lint(vault, rules=RULES_1_3).errors) == {"E_FRONTMATTER_UNPARSEABLE": 1}


# --- type -------------------------------------------------------------------------


def test_a_type_outside_the_document_types_is_a_warning_not_an_error(tmp_path: Path) -> None:
    vault = _write(
        tmp_path / "v",
        {
            "A.md": _note(type="module-doc", last_updated="2026-09-01"),
            "B.md": _note(type="runbook", last_updated="2026-09-01"),
            "C.md": _note(last_updated="2026-09-01"),
            "D.md": "---\ntype: [a, b]\nlast_updated: 2026-09-01\n---\nx\n",
        },
    )
    report = lint(vault, rules=RULES_1_3)
    assert report.errors == []
    assert sorted(_findings(report, "W_TYPE_UNKNOWN")) == ["A.md", "D.md"]
    assert report.ok


def test_every_document_type_of_the_set_is_accepted(tmp_path: Path) -> None:
    files = {f"{t}.md": _note(type=t, last_updated="2026-09-01") for t in sorted(vault_lint.DOCUMENT_TYPES)}
    assert lint(_write(tmp_path / "v", files), rules=RULES_1_3).warnings == []
    assert vault_lint.DOCUMENT_TYPES == {
        "hub", "service", "architecture", "flow", "runbook", "convention",
        "decision", "reference", "note", "pendientes", "memory",
    }  # fmt: skip


# --- scope ------------------------------------------------------------------------


def test_a_scope_of_another_vault_is_an_error(tmp_path: Path) -> None:
    vault = _write(
        tmp_path / "v",
        {
            "vault.yaml": "slug: alpha\n",
            "Own.md": _note(type="convention", scope="alpha", last_updated="2026-09-01"),
            "Global.md": _note(type="convention", scope="global", last_updated="2026-09-01"),
            "Foreign.md": _note(type="convention", scope="beta", last_updated="2026-09-01"),
            "Many.md": _note(type="convention", scope="[alpha, gamma]", last_updated="2026-09-01"),
        },
    )
    errors = lint(vault, rules=RULES_1_3).errors
    assert [(e["code"], e["file"], e["target"]) for e in errors] == [
        ("E_SCOPE_FOREIGN", "Foreign.md", "beta"),
        ("E_SCOPE_FOREIGN", "Many.md", "gamma"),
    ]


def test_scopes_the_vault_declares_are_accepted(tmp_path: Path) -> None:
    vault = _write(
        tmp_path / "v",
        {
            "vault.yaml": "slug: personal\nscopes: [claude-code]\n",
            "Note.md": _note(type="reference", scope="claude-code", last_updated="2026-09-01"),
        },
    )
    assert lint(vault, rules=RULES_1_3).errors == []


def test_without_a_slug_there_is_nothing_to_compare_a_scope_with(tmp_path: Path) -> None:
    vault = _write(tmp_path / "v", {"Note.md": _note(type="note", scope="whatever", last_updated="2026-09-01")})
    assert lint(vault, rules=RULES_1_3).errors == []


# --- paths ------------------------------------------------------------------------


def test_paths_that_differ_only_by_case_collide() -> None:
    found = vault_lint._path_findings(["Notes/Alpha.md", "notes/alpha.md", "Notes/Other.md"])
    assert [(f["code"], f["file"]) for f in found] == [("E_PATH_CASE_COLLISION", "Notes")]
    assert "Notes, notes" in found[0]["message"]


def test_a_directory_that_differs_by_case_collides_once_not_per_file() -> None:
    found = vault_lint._path_findings(["Docs/a.md", "Docs/b.md", "docs/c.md"])
    assert [(f["code"], f["file"]) for f in found] == [("E_PATH_CASE_COLLISION", "Docs")]


def test_two_spellings_of_one_name_in_different_unicode_forms_collide() -> None:
    nfc = unicodedata.normalize("NFC", "México.md")
    nfd = unicodedata.normalize("NFD", "México.md")
    assert nfc != nfd
    found = vault_lint._path_findings([nfc, nfd])
    assert {f["code"] for f in found} == {"E_PATH_CASE_COLLISION", "E_PATH_NOT_NFC"}


def test_a_path_that_is_not_nfc_is_an_error(tmp_path: Path) -> None:
    nfd = unicodedata.normalize("NFD", "Cliente México")
    vault = _write(tmp_path / "v", {f"{nfd}/Hub.md": _note(type="hub", last_updated="2026-09-01")})
    [error] = [e for e in lint(vault, rules=RULES_1_3).errors if e["code"] == "E_PATH_NOT_NFC"]
    assert unicodedata.normalize("NFC", error["file"]) == "Cliente México"
    assert error["file"] == nfd  # the shallowest offending path, once


def test_distinct_names_and_nfc_paths_are_clean() -> None:
    assert vault_lint._path_findings(["A/x.md", "A/y.md", "B/x.md", "Cliente México/Hub.md"]) == []


# --- runbooks ---------------------------------------------------------------------


def _runbook(**fields: str) -> str:
    base = {"type": "runbook", "id": "RB-002", "service": "portal", "status": "draft", "last_updated": "2026-09-01"}
    base.update(fields)
    return _note(**base)


def _vault_with_services(tmp_path: Path, files: dict[str, str], manifest: str = "slug: v\n") -> Path:
    return _write(
        tmp_path / "v",
        {
            "vault.yaml": manifest,
            "Runbooks/services.json": '{"services": [{"slug": "portal", "aliases": ["web"]}]}',
            **files,
        },
    )


def test_a_valid_runbook_passes(tmp_path: Path) -> None:
    vault = _vault_with_services(tmp_path, {"Runbooks/Auth/RB-002 One.md": _runbook(service="Web")})
    assert lint(vault, rules=RULES_1_3).errors == []


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"id": "RB-12"}, "E_RUNBOOK_ID_INVALID"),
        ({"id": "rb-012"}, "E_RUNBOOK_ID_INVALID"),
        ({"id": "RB-0123"}, "E_RUNBOOK_ID_INVALID"),
        ({"status": "active"}, "E_RUNBOOK_STATUS_INVALID"),
        ({"status": "complete"}, "E_RUNBOOK_STATUS_INVALID"),
        ({"service": "nowhere"}, "E_RUNBOOK_SERVICE_UNKNOWN"),
    ],
)
def test_a_runbook_the_memory_server_would_skip_is_an_error(
    tmp_path: Path, fields: dict[str, str], code: str
) -> None:
    vault = _vault_with_services(tmp_path, {"Runbooks/Auth/RB-002 One.md": _runbook(**fields)})
    assert [e["code"] for e in lint(vault, rules=RULES_1_3).errors] == [code]


def test_a_runbook_without_id_status_or_service_fails_each_check(tmp_path: Path) -> None:
    vault = _vault_with_services(
        tmp_path, {"Runbooks/Bare.md": "---\ntype: runbook\nlast_updated: 2026-09-01\n---\nx\n"}
    )
    assert _codes(lint(vault, rules=RULES_1_3).errors) == {
        "E_RUNBOOK_ID_INVALID": 1,
        "E_RUNBOOK_STATUS_INVALID": 1,
        "E_RUNBOOK_SERVICE_UNKNOWN": 1,
    }


def test_runbook_rules_are_anchored_in_the_runbooks_directory(tmp_path: Path) -> None:
    vault = _vault_with_services(
        tmp_path,
        {
            "Work/Project/Runbooks/Loose.md": _note(type="runbook", status="complete", last_updated="2026-09-01"),
            "Runbooks/Templates/T.md": _note(type="runbook", last_updated="2026-09-01"),
            "Runbooks/Tagged.md": _note(type="runbook", tags="[template]", last_updated="2026-09-01"),
            "Runbooks/Playbook.md": _note(type="playbook", last_updated="2026-09-01"),
        },
    )
    assert lint(vault, rules=RULES_1_3).errors == []


def test_the_runbooks_directory_comes_from_the_manifest(tmp_path: Path) -> None:
    vault = _write(
        tmp_path / "v",
        {
            "vault.yaml": "slug: v\nrunbooks: {dir: Ops/Runbooks}\n",
            "Ops/Runbooks/services.json": '{"services": [{"slug": "portal"}]}',
            "Ops/Runbooks/One.md": _runbook(id="bad"),
            "Runbooks/Elsewhere.md": _note(type="runbook", last_updated="2026-09-01"),
        },
    )
    errors = lint(vault, rules=RULES_1_3).errors
    assert [(e["code"], e["file"]) for e in errors] == [("E_RUNBOOK_ID_INVALID", "Ops/Runbooks/One.md")]


def test_the_services_file_can_be_named_by_the_manifest(tmp_path: Path) -> None:
    vault = _write(
        tmp_path / "v",
        {
            "vault.yaml": "slug: v\nrunbooks: {services_file: Runbooks/map.json}\n",
            "Runbooks/map.json": '{"services": [{"slug": "portal"}]}',
            "Runbooks/One.md": _runbook(),
        },
    )
    assert lint(vault, rules=RULES_1_3).errors == []


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"services": {}}', '{"services": []}', '{"services": [{"aliases": ["a"]}]}',
     '{"services": [{"slug": "p", "aliases": "a"}]}'],
)  # fmt: skip
def test_a_services_map_of_the_wrong_shape_is_one_error_and_no_service_checks(tmp_path: Path, content: str) -> None:
    vault = _write(
        tmp_path / "v",
        {
            "vault.yaml": "slug: v\n",
            "Runbooks/services.json": content,
            "Runbooks/One.md": _runbook(service="anything"),
            "Runbooks/Two.md": _runbook(id="RB-003", service="other"),
        },
    )
    errors = lint(vault, rules=RULES_1_3).errors
    assert [(e["code"], e["file"]) for e in errors] == [("E_SERVICES_MAP_INVALID", "Runbooks/services.json")]


def test_without_a_services_map_the_service_is_not_checked_and_the_vault_is_warned_once(tmp_path: Path) -> None:
    vault = _write(
        tmp_path / "v",
        {
            "vault.yaml": "slug: v\n",
            "Runbooks/One.md": _runbook(service="anything"),
            "Runbooks/Two.md": _runbook(id="RB-003", service="other"),
        },
    )
    report = lint(vault, rules=RULES_1_3)
    assert report.errors == []
    assert _findings(report, "W_SERVICES_MAP_MISSING") == ["Runbooks/services.json"]


def test_a_vault_without_runbooks_needs_no_services_map(tmp_path: Path) -> None:
    vault = _write(tmp_path / "v", {"Note.md": _note(type="note", last_updated="2026-09-01")})
    assert lint(vault, rules=RULES_1_3).warnings == []


@pytest.mark.parametrize("value", ["/etc", "../outside", "", "   "])
def test_a_runbooks_directory_outside_the_vault_is_a_manifest_error(tmp_path: Path, value: str) -> None:
    vault = _write(
        tmp_path / "v",
        {"vault.yaml": f"slug: v\nrunbooks: {{dir: '{value}'}}\n", "Note.md": _note(type="note", last_updated="2026-09-01")},
    )
    [error] = lint(vault, rules=RULES_1_3).errors
    assert (error["code"], error["file"], error["target"]) == ("E_MANIFEST_INVALID", "vault.yaml", "runbooks.dir")


# --- wikilinks and code ---------------------------------------------------------


def _unresolved(report: Any) -> set[str]:
    return {e["target"] for e in report.errors if e["code"] == "E_WIKILINK_UNRESOLVED"}


def test_wikilinks_in_code_are_ignored_by_the_1_3_rules_only() -> None:
    assert _unresolved(lint(CLEAN, rules=RULES_1_3)) == set()
    assert _unresolved(lint(CLEAN, rules=RULES_1_2)) == {
        "Not A Note",
        "Also Not A Note",
        "Tilde Fence",
        "Placeholder Target",
    }


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("see `[[A]]` here", set()),
        ("see ``[[A]] and `[[B]]` ``", set()),
        ("```\n[[A]]\n```\n[[B]]\n", {"B"}),
        ("````md\n```\n[[A]]\n```\n[[B]]\n````\n[[C]]\n", {"C"}),
        ("~~~\n[[A]]\n~~~\n", set()),
        ("```\n[[A]]\n", set()),  # an unclosed fence runs to the end of the note
        ("  ```\n[[A]]\n  ```\n[[B]]\n", {"B"}),
        ("a ` lone backtick\n\n[[A]]\n", {"A"}),
        ("[[A]] and `code` and [[B]]", {"A", "B"}),
        ("```\r\n[[A]]\r\n```\r\n[[B]]\r\n", {"B"}),
    ],
)
def test_strip_code_blanks_fences_and_inline_spans_and_nothing_else(body: str, expected: set[str]) -> None:
    stripped = vault_lint._strip_code(body)
    from knowledge.graph import extract_wikilinks

    assert set(extract_wikilinks(stripped)) == expected


# --- index.exclude ---------------------------------------------------------------


def test_excluded_files_are_not_ambiguous_stems_and_do_not_originate_links() -> None:
    report = lint(CLEAN, rules=RULES_1_3)
    assert report.warnings == []
    unfiltered = lint(CLEAN, rules=RULES_1_2)
    ambiguous = [w for w in unfiltered.warnings if w["code"] == "W_WIKILINK_AMBIGUOUS_STEM"]
    assert [w["stem"] for w in ambiguous] == ["pendientes"]
    assert len(ambiguous[0]["files"]) == 3


CONTEXTS_VAULT = Path(__file__).resolve().parent / "fixtures" / "vaults" / "contexts"
FRONTMATTER_CODES = {
    "E_FRONTMATTER_UNPARSEABLE",
    "E_FRONTMATTER_NOT_MAPPING",
    "E_FRONTMATTER_LAST_UPDATED",
    "E_SCOPE_FOREIGN",
    "W_TYPE_UNKNOWN",
}


def _contexts_vault(tmp_path: Path, exclude: str | None) -> Path:
    """A copy of the ``contexts`` fixture: its ``Contexts/<app>/CLAUDE.md`` files have no frontmatter."""
    vault = tmp_path / "vault"
    shutil.copytree(CONTEXTS_VAULT, vault)
    if exclude is not None:
        manifest = vault / "vault.yaml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + f'index:\n  exclude: ["{exclude}"]\n', encoding="utf-8")
    return vault


def _frontmatter_findings_under(report: Any, prefix: str) -> list[tuple[str, str]]:
    return sorted(
        (e["code"], e["file"])
        for e in [*report.errors, *report.warnings]
        if e["code"] in FRONTMATTER_CODES and e["file"].startswith(prefix)
    )


def test_excluded_files_skip_the_frontmatter_rules_of_1_3(tmp_path: Path) -> None:
    vault = _contexts_vault(tmp_path, "Contexts/**")
    assert _frontmatter_findings_under(lint(vault, rules=RULES_1_3), "Contexts/") == []


def test_the_same_files_outside_the_exclude_keep_getting_the_rules(tmp_path: Path) -> None:
    without = _contexts_vault(tmp_path / "without", None)
    elsewhere = _contexts_vault(tmp_path / "elsewhere", "_templates/**")
    expected = [
        ("E_FRONTMATTER_LAST_UPDATED", "Contexts/ledger-api/CLAUDE.md"),
        ("E_FRONTMATTER_LAST_UPDATED", "Contexts/ledger-web/CLAUDE.md"),
        ("E_FRONTMATTER_LAST_UPDATED", "Contexts/notes-cli/CLAUDE.md"),
    ]
    for vault in (without, elsewhere):
        assert _frontmatter_findings_under(lint(vault, rules=RULES_1_3), "Contexts/") == expected


@pytest.mark.parametrize(
    ("text", "code", "counted_by_1_2"),
    [
        ("---\ntype: nonsense\nscope: elsewhere\n---\nbody\n", "E_SCOPE_FOREIGN", False),
        ("---\ntype: nonsense\n---\nbody\n", "W_TYPE_UNKNOWN", False),
        ("---\nkey: [unclosed\n---\nbody\n", "E_FRONTMATTER_UNPARSEABLE", True),
    ],
)
def test_every_frontmatter_rule_is_skipped_for_an_excluded_file_in_1_3_only(
    tmp_path: Path, text: str, code: str, counted_by_1_2: bool
) -> None:
    vault = _write(
        tmp_path / "v",
        {
            "vault.yaml": 'slug: v\nindex: {exclude: ["Contexts/**"]}\n',
            "Contexts/a/CLAUDE.md": text,
            "Kept/CLAUDE.md": text,
        },
    )

    def files_with(rules: str) -> set[str]:
        report = lint(vault, rules=rules)
        return {e["file"] for e in [*report.errors, *report.warnings] if e["code"] == code}

    assert files_with(RULES_1_3) == {"Kept/CLAUDE.md"}
    # The rules reindex.validation counts never read the manifest, so nothing is exempt there.
    assert files_with(RULES_1_2) == ({"Contexts/a/CLAUDE.md", "Kept/CLAUDE.md"} if counted_by_1_2 else set())


def test_an_excluded_runbook_keeps_the_runbook_rules(tmp_path: Path) -> None:
    vault = _write(
        tmp_path / "v",
        {
            "vault.yaml": 'slug: v\nindex: {exclude: ["Runbooks/Drafts/**"]}\n',
            "Runbooks/Drafts/RB.md": _note(type="runbook", id="oops", status="draft", last_updated="2026-09-01"),
        },
    )
    assert "E_RUNBOOK_ID_INVALID" in _codes(lint(vault, rules=RULES_1_3).errors)


@pytest.mark.parametrize(
    ("pattern", "path", "excluded"),
    [
        ("_templates/**", "_templates/a.md", True),
        ("_templates/**", "_templates/x/y/a.md", True),
        ("_templates/**", "other/_templates/a.md", False),
        ("**/Pendientes.md", "Pendientes.md", True),
        ("**/Pendientes.md", "a/b/Pendientes.md", True),
        ("**/Pendientes.md", "a/Pendientes.md.bak", False),
        ("Archive", "Archive/x.md", True),
        ("*.md", "root.md", True),
        ("*.md", "dir/inner.md", False),
        ("a?.md", "ab.md", True),
        ("a?.md", "a/b.md", False),
    ],
)
def test_index_exclude_globs(pattern: str, path: str, excluded: bool) -> None:
    manifest = vault_lint._Manifest(exclude=(vault_lint._glob_regex(pattern),))
    assert manifest.excludes(path) is excluded


# --- the manifest ---------------------------------------------------------------


def test_a_broken_manifest_is_a_finding_not_a_crash(tmp_path: Path) -> None:
    vault = _write(
        tmp_path / "v",
        {"vault.yaml": "slug: [unclosed\n", "Note.md": _note(type="note", last_updated="2026-09-01")},
    )
    report = lint(vault, rules=RULES_1_3)
    [error] = report.errors
    assert (error["code"], error["file"]) == ("E_MANIFEST_INVALID", "vault.yaml")
    assert str(tmp_path) not in json.dumps(report.to_json())


@pytest.mark.parametrize(
    ("manifest", "target"),
    [("scopes: x\n", "scopes"), ("scopes: [1]\n", "scopes"), ("index: {exclude: x}\n", "index.exclude")],
)
def test_a_manifest_field_of_the_wrong_shape_names_itself(tmp_path: Path, manifest: str, target: str) -> None:
    vault = _write(tmp_path / "v", {"vault.yaml": f"slug: v\n{manifest}"})
    [error] = lint(vault, rules=RULES_1_3).errors
    assert (error["code"], error["target"]) == ("E_MANIFEST_INVALID", target)


def test_a_vault_without_manifest_lints_with_the_defaults(tmp_path: Path) -> None:
    vault = _write(tmp_path / "v", {"Note.md": _note(type="note", scope="x", last_updated="2026-09-01")})
    assert lint(vault, rules=RULES_1_3).errors == []


def test_the_report_is_stable_and_free_of_absolute_paths(tmp_path: Path) -> None:
    vault = tmp_path / "copy"
    shutil.copytree(LEGACY, vault)
    first = json.dumps(lint(vault, rules=RULES_1_3).to_json(), sort_keys=True)
    second = json.dumps(lint(vault, rules=RULES_1_3).to_json(), sort_keys=True)
    assert first == second
    assert str(tmp_path) not in first


# --- fix ------------------------------------------------------------------------------


def _legacy(tmp_path: Path) -> Path:
    vault = tmp_path / "legacy"
    shutil.copytree(LEGACY, vault)
    return vault


def _meta(path: Path) -> dict[str, Any]:
    return dict(frontmatter.load(str(path)).metadata)


def test_fix_moves_each_source_into_last_updated(tmp_path: Path) -> None:
    vault = _legacy(tmp_path)

    outcome = fix(vault)

    by_file = {f["file"]: f for f in outcome.fixed}
    assert {k: (v["from"], v["value"]) for k, v in by_file.items()} == {
        "updated.md": ("updated", "2026-05-01"),
        "review.md": ("last_review", "2026-05-02"),
        "verified_block.md": ("last_verified.date", "2026-09-07"),
        "verified_flow.md": ("last_verified.date", "2026-09-07"),
        "verified_date_only.md": ("last_verified.date", "2026-09-08"),
        "both_sources.md": ("updated", "2026-03-03"),
        "blank_with_review.md": ("last_review", "2026-04-05"),
    }
    assert all(f["rule"] == "E_FRONTMATTER_LAST_UPDATED" and f["to"] == "last_updated" for f in outcome.fixed)
    assert [f["file"] for f in outcome.fixed] == sorted(by_file)


def test_fix_leaves_for_a_person_what_has_no_date_to_take(tmp_path: Path) -> None:
    vault = _legacy(tmp_path)
    before = _tree_digest(vault)

    outcome = fix(vault)

    assert {r["file"]: r["reason"] for r in outcome.remaining} == {
        "bare.md": "no_source",
        "scalar_verified.md": "no_source",
        "placeholder.md": "source_invalid",
        "conflict.md": "edit_unsafe",
    }
    after = _tree_digest(vault)
    for rel in outcome.remaining:
        assert after[rel["file"]] == before[rel["file"]]
    assert after["already.md"] == before["already.md"]
    assert after["vault.yaml"] == before["vault.yaml"]


def test_fix_renames_a_key_in_place_and_keeps_every_other_byte(tmp_path: Path) -> None:
    vault = _legacy(tmp_path)
    before = (vault / "review.md").read_text(encoding="utf-8")

    fix(vault)

    after = (vault / "review.md").read_text(encoding="utf-8")
    assert after == before.replace("last_review:", "last_updated:")
    assert "last_updated: 2026-05-02   # reviewed by hand" in after


def test_fix_never_touches_the_body(tmp_path: Path) -> None:
    vault = _legacy(tmp_path)
    before = frontmatter.load(str(vault / "updated.md")).content

    fix(vault)

    text = (vault / "updated.md").read_text(encoding="utf-8")
    assert frontmatter.loads(text).content == before
    assert text.endswith("\n---\n\nlast_review: 1999-01-01\n")
    assert "The body says updated: 2000-01-01" in text
    assert _meta(vault / "updated.md") == {"type": "note", "last_updated": _meta(vault / "updated.md")["last_updated"], "tags": ["a", "b"]}
    assert str(_meta(vault / "updated.md")["last_updated"]) == "2026-05-01"


def test_fix_keeps_the_rest_of_last_verified_as_verified_against(tmp_path: Path) -> None:
    vault = _legacy(tmp_path)

    fix(vault)

    assert (vault / "verified_block.md").read_text(encoding="utf-8") == (
        "---\n"
        "type: architecture\n"
        "last_updated: 2026-09-07\n"
        "verified_against:\n"
        "  web: 004c448\n"
        '  api: "1234567"\n'
        "tags: [x]\n"
        "---\n\n# Verified block\n"
    )
    assert (vault / "verified_flow.md").read_text(encoding="utf-8") == (
        "---\n"
        "type: architecture\n"
        "last_updated: 2026-09-07\n"
        "verified_against: { web: a19eef6, api: 004c448 }\n"
        "tags: [x]\n"
        "---\n\n# Verified flow\n"
    )
    assert (vault / "verified_date_only.md").read_text(encoding="utf-8") == (
        "---\ntype: architecture\nlast_updated: 2026-09-08\n---\n\n# Verified date only\n"
    )
    meta = _meta(vault / "verified_block.md")
    assert meta["verified_against"] == {"web": "004c448", "api": "1234567"}
    assert "last_verified" not in meta


def test_fix_replaces_an_unreadable_last_updated_and_drops_the_alias_it_used(tmp_path: Path) -> None:
    vault = _legacy(tmp_path)

    fix(vault)

    assert (vault / "both_sources.md").read_text(encoding="utf-8") == (
        "---\ntype: note\nlast_updated: 2026-03-03\nlast_review: 2026-04-04\n---\n\n# Both sources\n"
    )
    assert (vault / "blank_with_review.md").read_text(encoding="utf-8") == (
        "---\ntype: note\nlast_updated: 2026-04-05\n---\n\n# Blank last_updated\n"
    )


def test_fix_keeps_crlf_line_endings(tmp_path: Path) -> None:
    vault = tmp_path / "v"
    vault.mkdir()
    (vault / "Crlf.md").write_bytes(
        b"---\r\ntype: architecture\r\nlast_verified:\r\n  web: abc1234\r\n  date: 2026-09-07\r\n---\r\n\r\nbody\r\n"
    )
    (vault / "Rename.md").write_bytes(b"---\r\ntype: note\r\nupdated: 2026-01-02\r\n---\r\nbody\r\n")

    fix(vault)

    assert (vault / "Crlf.md").read_bytes() == (
        b"---\r\ntype: architecture\r\nlast_updated: 2026-09-07\r\nverified_against:\r\n  web: abc1234\r\n---\r\n\r\nbody\r\n"
    )
    assert (vault / "Rename.md").read_bytes() == b"---\r\ntype: note\r\nlast_updated: 2026-01-02\r\n---\r\nbody\r\n"


def test_fix_is_idempotent(tmp_path: Path) -> None:
    vault = _legacy(tmp_path)
    first = fix(vault)
    state = _tree_digest(vault)

    second = fix(vault)

    assert first.fixed and second.fixed == []
    assert _tree_digest(vault) == state
    assert second.remaining == first.remaining


def test_after_fix_only_the_remaining_notes_still_fail_the_date_rule(tmp_path: Path) -> None:
    vault = _legacy(tmp_path)
    outcome = fix(vault)

    errors = lint(vault, rules=RULES_1_3).errors

    assert sorted(e["file"] for e in errors if e["code"] == "E_FRONTMATTER_LAST_UPDATED") == sorted(
        r["file"] for r in outcome.remaining
    )


def test_fix_skips_notes_it_cannot_read(tmp_path: Path) -> None:
    vault = _write(
        tmp_path / "v",
        {"Bad.md": "---\nkey: [unclosed\nupdated: 2026-01-01\n---\nbody\n"},
    )
    (vault / "Binary.md").write_bytes(b"---\nupdated: 2026-01-01\n---\n\xff\xfe\n")
    before = _tree_digest(vault)

    outcome = fix(vault)

    assert (outcome.fixed, outcome.remaining) == ([], [])
    assert _tree_digest(vault) == before


def test_fix_skips_hidden_directories(tmp_path: Path) -> None:
    vault = _write(tmp_path / "v", {".obsidian/Hidden.md": "---\nupdated: 2026-01-01\n---\nx\n"})
    before = _tree_digest(vault)
    assert fix(vault).fixed == []
    assert _tree_digest(vault) == before


def test_fix_refuses_a_path_that_is_not_a_directory(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeConfigError) as raised:
        fix(tmp_path / "nope")
    assert raised.value.code == "E_LINT_PATH_NOT_DIR"


def test_fix_outcome_serialises_to_the_two_keys() -> None:
    assert FixOutcome().to_json() == {"fixed": [], "remaining": []}


def test_only_the_fix_writer_opens_a_file_for_writing() -> None:
    tree = ast.parse(Path(vault_lint.__file__).read_text(encoding="utf-8"))
    writers: set[str] = set()
    for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in {"write_text", "write_bytes", "unlink", "mkdir", "rmtree", "rename", "replace", "touch"}:
                writers.add(function.name)
            if name == "open":
                modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
                modes += [k.value.value for k in node.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)]
                if any(set("wax+") & set(str(m)) for m in modes):
                    writers.add(function.name)
    assert writers == {"_write_text"}


def test_the_server_never_reaches_the_fixer() -> None:
    for module in (server,):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "vault_lint.fix" not in source
        assert "import fix" not in source


# --- the command line ---------------------------------------------------------------


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    rc = cli.main(["vault-lint", *argv])
    out = capsys.readouterr().out
    return rc, json.loads(out) if out else {}


def test_cli_checks_the_1_3_rules_by_default(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    vault = _legacy(tmp_path)
    rc, report = _cli(capsys, str(vault))
    assert rc == 1
    assert report["rules"] == "1.3"
    assert "E_FRONTMATTER_LAST_UPDATED" in _codes(report["errors"])
    assert "fixed" not in report


def test_cli_rules_1_2_keeps_the_old_numbers(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    vault = _legacy(tmp_path)
    rc, report = _cli(capsys, str(vault), "--rules", "1.2")
    assert (rc, report["rules"], report["errors"]) == (0, "1.2", [])


def test_cli_without_fix_leaves_the_tree_alone(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    vault = _legacy(tmp_path)
    before = _tree_digest(vault)
    _cli(capsys, str(vault))
    assert _tree_digest(vault) == before


def test_cli_fix_reports_what_it_fixed_and_what_remains(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    vault = _legacy(tmp_path)

    rc, report = _cli(capsys, str(vault), "--fix")

    assert rc == 1  # the notes with no date to take still fail
    assert len(report["fixed"]) == 7
    assert {r["file"] for r in report["remaining"]} == {e["file"] for e in report["errors"] if e["code"] == "E_FRONTMATTER_LAST_UPDATED"}
    assert list(report) == sorted(report)

    rc, again = _cli(capsys, str(vault), "--fix")
    assert again["fixed"] == [] and again["remaining"] == report["remaining"]


def test_cli_fix_exits_zero_when_nothing_is_left(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    vault = _write(tmp_path / "v", {"A.md": _note(type="note", updated="2026-01-01"), "B.md": _note(type="note", last_review="2026-01-02")})

    rc, report = _cli(capsys, str(vault), "--fix")

    assert (rc, report["errors"], report["remaining"]) == (0, [], [])
    assert len(report["fixed"]) == 2


def test_cli_fix_needs_the_1_3_rules(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    vault = _legacy(tmp_path)
    before = _tree_digest(vault)
    with pytest.raises(SystemExit) as raised:
        cli.main(["vault-lint", str(vault), "--rules", "1.2", "--fix"])
    assert raised.value.code == 2
    assert "--fix needs --rules 1.3" in capsys.readouterr().err
    assert _tree_digest(vault) == before


def test_cli_fix_on_a_missing_path_exits_2(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    rc = cli.main(["vault-lint", str(tmp_path / "nope"), "--fix"])
    captured = capsys.readouterr()
    assert rc == 2 and captured.out == ""
    assert captured.err.startswith("E_LINT_PATH_NOT_DIR: ")


def test_fix_never_writes_through_a_symbolic_link(tmp_path: Path) -> None:
    outside = _write(tmp_path / "outside", {"Target.md": _note(type="note", updated="2026-01-01")})
    vault = _write(tmp_path / "v", {"A.md": _note(type="note", updated="2026-01-02")})
    (vault / "Linked.md").symlink_to(outside / "Target.md")
    before = _tree_digest(outside)

    outcome = fix(vault)

    assert [row["file"] for row in outcome.fixed] == ["A.md"]
    assert outcome.remaining == [{"file": "Linked.md", "rule": "E_FRONTMATTER_LAST_UPDATED", "reason": "edit_unsafe"}]
    assert _tree_digest(outside) == before
    assert (vault / "Linked.md").is_symlink()


def test_fix_replaces_a_note_whole_and_keeps_its_mode(tmp_path: Path) -> None:
    vault = _write(tmp_path / "v", {"A.md": _note(type="note", updated="2026-01-02")})
    note = vault / "A.md"
    note.chmod(0o640)

    fix(vault)

    assert _meta(note)["last_updated"] is not None
    assert oct(note.stat().st_mode & 0o777) == oct(0o640)
    assert sorted(p.name for p in vault.iterdir()) == ["A.md"], "no temporary file is left behind"
