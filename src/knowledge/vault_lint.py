"""Vault linter: frontmatter that parses, wikilinks that resolve, and the vault rules.

``lint`` walks a vault directory and reports, without touching it, the same
problems the indexer would silently skip or leave dangling. Wikilinks resolve
through ``graph.resolve_target`` over the same ``graph.LinkIndex`` the indexer
builds, so the linter and the index agree on what a link points to. A
finding names the parsed target (no alias, heading or block), once per file;
a link to a non-Markdown file that exists (a ``.canvas``, an image) is not a
finding, even though it is no edge of the graph.

Two rule sets exist. ``RULES_1_2`` is the one ``reindex.validation`` counts
and never changes during the 1.x series: unparseable frontmatter, wikilinks
that resolve to nothing and stems that repeat. ``RULES_1_3`` adds the vault
rules on top (no schema library, plain code): a readable ISO
``last_updated``, a known document ``type``, a ``scope`` the vault owns, NFC
and case-unique paths, runbooks the memory server can index, and wikilinks
that ignore code. ``lint`` defaults to ``RULES_1_2`` so every existing caller
keeps its numbers; the command line asks for ``RULES_1_3``.

The report is plain data (``LintReport.to_json``): every path is relative to
the linted directory and every list is sorted, so two runs over the same tree
produce the same bytes.

``fix`` is the one function that writes. It only moves a date a note already
carries (``updated``, ``last_review`` or ``last_verified.date``) into
``last_updated``, editing the frontmatter lines in place and never the body,
and it refuses any edit whose re-parse differs from the intended change.
Nothing else in the module opens a file for writing.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import frontmatter

from knowledge import settings
from knowledge.errors import (
    E_FRONTMATTER_LAST_UPDATED,
    E_FRONTMATTER_NOT_MAPPING,
    E_FRONTMATTER_UNPARSEABLE,
    E_LINT_PATH_NOT_DIR,
    E_LINT_RULES_UNKNOWN,
    E_MANIFEST_INVALID,
    E_PATH_CASE_COLLISION,
    E_PATH_NOT_NFC,
    E_RUNBOOK_ID_INVALID,
    E_RUNBOOK_SERVICE_UNKNOWN,
    E_RUNBOOK_STATUS_INVALID,
    E_SCOPE_FOREIGN,
    E_SERVICES_MAP_INVALID,
    E_WIKILINK_UNRESOLVED,
    W_SERVICES_MAP_MISSING,
    W_TYPE_UNKNOWN,
    W_WIKILINK_AMBIGUOUS_STEM,
    KnowledgeConfigError,
)
from knowledge.graph import LinkIndex, extract_wikilinks, parse_target, resolve_target

#: The rules ``reindex.validation`` counts; frozen for the whole 1.x series.
RULES_1_2 = "1.2"
#: The 1.2 rules plus the vault rules.
RULES_1_3 = "1.3"
RULESETS = (RULES_1_2, RULES_1_3)
LATEST_RULES = RULES_1_3

#: The closed set of document types; anything else is a warning.
DOCUMENT_TYPES = frozenset(
    {
        "hub",
        "service",
        "architecture",
        "flow",
        "runbook",
        "convention",
        "decision",
        "reference",
        "note",
        "pendientes",
        "memory",
    }
)
GLOBAL_SCOPE = "global"
DEFAULT_RUNBOOKS_DIR = "Runbooks"
SERVICES_FILE_NAME = "services.json"
_RUNBOOK_ID = re.compile(r"RB-[0-9]{3}")
_RUNBOOK_STATUSES = frozenset({"draft", "verified", "outdated"})
_TEMPLATES_DIR = "Templates"


@dataclass
class LintReport:
    """Outcome of linting one vault directory."""

    vault: str
    files: int = 0
    rules: str = RULES_1_2
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_json(self) -> dict[str, Any]:
        return {
            "vault": self.vault,
            "files": self.files,
            "rules": self.rules,
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass
class FixOutcome:
    """What ``fix`` changed and what it left for a person.

    ``fixed`` holds ``{file, rule, from, to, value}`` per note whose date
    moved; ``remaining`` holds ``{file, rule, reason}`` per note that still
    has no readable ``last_updated``, with ``reason`` one of ``no_source``
    (no key to take a date from), ``source_invalid`` (the keys are there and
    none is an ISO date) or ``edit_unsafe`` (the frontmatter layout is one
    the in-place edit does not trust).
    """

    fixed: list[dict[str, Any]] = field(default_factory=list)
    remaining: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"fixed": self.fixed, "remaining": self.remaining}


# ---------------------------------------------------------------------------
# Vault walk
# ---------------------------------------------------------------------------


def _markdown_files(root: Path) -> list[Path]:
    """Sorted ``*.md`` files under ``root``, skipping any dot-prefixed segment."""
    found: list[Path] = []
    for path in sorted(root.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        found.append(path)
    return found


def _attachment_keys(root: Path) -> set[str]:
    """Folded paths and names of the non-Markdown files under ``root``, skipping dot-prefixed segments."""
    keys: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or name.lower().endswith(".md"):
                continue
            rel = (Path(dirpath) / name).relative_to(root)
            keys.add(rel.as_posix().casefold())
            keys.add(name.casefold())
    return keys


def _names_attachment(target: str, attachments: set[str]) -> bool:
    folded = target.casefold()
    return folded in attachments or folded.rsplit("/", 1)[-1] in attachments


def _require_dir(root: Path) -> None:
    if not root.is_dir():
        raise KnowledgeConfigError(
            E_LINT_PATH_NOT_DIR,
            f"vault-lint needs a directory; {root.name or str(root)!r} is not one",
        )


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """A gitignore-style glob as a regex over vault-relative POSIX paths.

    ``**/`` matches any number of directories (also none), a trailing ``/**``
    everything below a directory, ``*`` and ``?`` stay inside one segment.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("/.*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def _safe_rel(value: Any, default: str) -> str | None:
    """``value`` as a vault-relative POSIX path, ``default`` when absent, ``None`` when unusable."""
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        return None
    path = PurePosixPath("/".join(value.strip().split("\\")))
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts == (".",):
        return None
    return path.as_posix()


def _matches_any(patterns: Iterable[re.Pattern[str]], rel: str) -> bool:
    """Whether ``rel``, or a directory above it, matches one of the compiled ``index.exclude`` patterns."""
    parts = rel.split("/")
    prefixes = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    return any(rx.fullmatch(p) for rx in patterns for p in prefixes)


def index_exclude_matcher(patterns: Iterable[str]) -> Callable[[str], bool]:
    """A predicate over vault-relative POSIX paths: does one of the ``index.exclude`` globs cover it.

    The rule the linter applies to its stem and link checks and, with the 1.3
    rules, to the frontmatter checks; the review queue (``get_stale_docs`` and
    the weekly report) calls it so that what the lint does not hold to the
    frontmatter rules is never queued for review either.
    """
    compiled = tuple(_glob_regex(g.strip()) for g in patterns if g.strip())
    return lambda rel: _matches_any(compiled, rel)


@dataclass(frozen=True)
class _Manifest:
    """What the vault rules read from ``vault.yaml``."""

    slug: str | None = None
    scopes: frozenset[str] = frozenset()
    exclude: tuple[re.Pattern[str], ...] = ()
    runbooks_dir: str = DEFAULT_RUNBOOKS_DIR
    services_file: str = f"{DEFAULT_RUNBOOKS_DIR}/{SERVICES_FILE_NAME}"

    def excludes(self, rel: str) -> bool:
        """Whether ``rel``, or a directory above it, matches an ``index.exclude`` pattern."""
        return _matches_any(self.exclude, rel)

    @property
    def accepted_scopes(self) -> frozenset[str]:
        owned = {GLOBAL_SCOPE} | set(self.scopes)
        if self.slug:
            owned.add(self.slug)
        return frozenset(owned)


def _read_manifest(root: Path, report: LintReport) -> _Manifest:
    """The rule-relevant part of ``root/vault.yaml``; a broken field becomes a finding, not a crash."""
    try:
        raw = settings.read_vault_manifest(root)
    except KnowledgeConfigError:
        report.errors.append(
            {
                "code": E_MANIFEST_INVALID,
                "file": settings.VAULT_MANIFEST_NAME,
                "target": "",
                "message": f"{settings.VAULT_MANIFEST_NAME} is not a readable YAML mapping",
            }
        )
        return _Manifest()

    def invalid(where: str, why: str) -> None:
        report.errors.append(
            {
                "code": E_MANIFEST_INVALID,
                "file": settings.VAULT_MANIFEST_NAME,
                "target": where,
                "message": f"{where} {why}",
            }
        )

    slug = raw.get("slug")
    slug = slug.strip() if isinstance(slug, str) and slug.strip() else None

    scopes: frozenset[str] = frozenset()
    declared_scopes = raw.get("scopes")
    if declared_scopes is not None:
        if isinstance(declared_scopes, list) and all(isinstance(s, str) and s.strip() for s in declared_scopes):
            scopes = frozenset(s.strip() for s in declared_scopes)
        else:
            invalid("scopes", "must be a list of scope names")

    exclude: tuple[re.Pattern[str], ...] = ()
    index = raw.get("index")
    declared_exclude = index.get("exclude") if isinstance(index, dict) else None
    if declared_exclude is not None:
        if isinstance(declared_exclude, list) and all(isinstance(g, str) and g.strip() for g in declared_exclude):
            exclude = tuple(_glob_regex(g.strip()) for g in declared_exclude)
        else:
            invalid("index.exclude", "must be a list of glob patterns")

    runbooks_dir = DEFAULT_RUNBOOKS_DIR
    services_file: str | None = None
    declared_runbooks = raw.get("runbooks")
    if isinstance(declared_runbooks, dict):
        chosen = _safe_rel(declared_runbooks.get("dir"), DEFAULT_RUNBOOKS_DIR)
        if chosen is None:
            invalid("runbooks.dir", "must be a relative path inside the vault")
        else:
            runbooks_dir = chosen
        services_file = _safe_rel(declared_runbooks.get("services_file"), "")
        if services_file is None:
            invalid("runbooks.services_file", "must be a relative path inside the vault")
        elif not services_file:
            services_file = None
    return _Manifest(
        slug=slug,
        scopes=scopes,
        exclude=exclude,
        runbooks_dir=runbooks_dir,
        services_file=services_file or f"{runbooks_dir}/{SERVICES_FILE_NAME}",
    )


# ---------------------------------------------------------------------------
# 1.3 rules
# ---------------------------------------------------------------------------


def _is_iso_date(value: Any) -> bool:
    """Whether ``value`` reads as an ISO date the way ``get_stale_docs`` reads it."""
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def _last_updated_problem(meta: dict[str, Any]) -> str | None:
    """Why ``meta`` has no readable ``last_updated``, or ``None`` when it has one."""
    value = meta.get("last_updated")
    if value is None or not str(value).strip():
        return "last_updated is missing or blank"
    if not _is_iso_date(value):
        shown = str(value).strip()
        shown = shown if len(shown) <= 40 else f"{shown[:37]}..."
        return f"last_updated {shown!r} is not an ISO date"
    return None


def _finding(code: str, file: str, message: str, target: str = "") -> dict[str, Any]:
    return {"code": code, "file": file, "target": target, "message": message}


def _warning(code: str, file: str, message: str) -> dict[str, Any]:
    return {"code": code, "file": file, "message": message}


def _scope_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _frontmatter_findings(rel: str, meta: dict[str, Any], manifest: _Manifest, report: LintReport) -> None:
    problem = _last_updated_problem(meta)
    if problem is not None:
        report.errors.append(_finding(E_FRONTMATTER_LAST_UPDATED, rel, problem))

    doc_type = meta.get("type")
    if doc_type is not None and not (isinstance(doc_type, str) and doc_type in DOCUMENT_TYPES):
        shown = str(doc_type)
        shown = shown if len(shown) <= 40 else f"{shown[:37]}..."
        report.warnings.append(_warning(W_TYPE_UNKNOWN, rel, f"type {shown!r} is outside the document types"))

    if manifest.slug is not None and "scope" in meta:
        accepted = manifest.accepted_scopes
        for scope in _scope_values(meta["scope"]):
            if scope not in accepted:
                report.errors.append(
                    _finding(
                        E_SCOPE_FOREIGN,
                        rel,
                        f"scope {scope!r} is neither {manifest.slug!r}, {GLOBAL_SCOPE!r} nor declared in scopes",
                        target=scope,
                    )
                )


def _path_findings(paths: Iterable[str]) -> list[dict[str, Any]]:
    """Case and Unicode-form collisions among note paths, and paths that are not NFC.

    A directory counts as a path: ``A/x.md`` and ``a/y.md`` collide on a
    case-insensitive disk through their directory. Each problem is reported
    at its shallowest level, once.
    """
    prefixes: set[str] = set()
    for path in set(paths):
        parts = path.split("/")
        prefixes.update("/".join(parts[: n + 1]) for n in range(len(parts)))

    by_folded: dict[str, set[str]] = {}
    for prefix in prefixes:
        by_folded.setdefault(unicodedata.normalize("NFC", prefix).casefold(), set()).add(prefix)

    findings: list[dict[str, Any]] = []
    reported: set[str] = set()
    collided: set[str] = set()

    def under_reported(prefix: str) -> bool:
        parts = prefix.split("/")
        return any("/".join(parts[:n]) in reported for n in range(1, len(parts)))

    for prefix in sorted(prefixes, key=lambda p: (p.count("/"), p)):
        if under_reported(prefix):
            continue
        folded = unicodedata.normalize("NFC", prefix).casefold()
        spellings = by_folded[folded]
        if len(spellings) > 1 and folded not in collided:
            collided.add(folded)
            reported.update(spellings)
            first = min(spellings)
            names = ", ".join(sorted(spellings))
            findings.append(
                _finding(
                    E_PATH_CASE_COLLISION,
                    first,
                    f"paths that differ only by case or Unicode form: {names}",
                    target=first,
                )
            )
        if unicodedata.normalize("NFC", prefix) != prefix:
            reported.add(prefix)
            findings.append(
                _finding(E_PATH_NOT_NFC, prefix, "the path is not in Unicode normal form NFC", target=prefix)
            )
    return findings


_FENCE_OPEN = re.compile(r"[ \t]{0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
_PARAGRAPH_BREAK = re.compile(r"(\n[ \t]*\n)")


def _strip_code(text: str) -> str:
    """``text`` with fenced code blocks and inline code spans blanked out."""
    kept: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        if fence is None:
            opened = _FENCE_OPEN.match(line)
            if opened is not None:
                fence = opened.group(1)
                kept.append("")
            else:
                kept.append(line)
            continue
        closing = re.fullmatch(rf"[ \t]{{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line.rstrip("\r"))
        if closing is not None:
            fence = None
        kept.append("")
    pieces = _PARAGRAPH_BREAK.split("\n".join(kept))
    return "".join(piece if _PARAGRAPH_BREAK.fullmatch(piece) else _INLINE_CODE.sub(" ", piece) for piece in pieces)


def _load_services(root: Path, rel: str) -> tuple[str, frozenset[str]]:
    """The services map as ``(state, names)``; ``state`` is ``ok``, ``missing`` or ``invalid``.

    ``names`` holds every slug and alias, trimmed and lower-cased as the
    memory server folds a note's ``service``.
    """
    path = root / rel
    if not path.is_file():
        return "missing", frozenset()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "invalid", frozenset()
    entries = data.get("services") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return "invalid", frozenset()
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str):
            return "invalid", frozenset()
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            return "invalid", frozenset()
        for name in [entry["slug"], *aliases]:
            if name.strip():
                names.add(name.strip().casefold())
    return ("ok", frozenset(names)) if names else ("invalid", frozenset())


def _is_runbook(rel: str, meta: dict[str, Any], manifest: _Manifest) -> bool:
    """A ``type: runbook`` note under ``runbooks.dir`` that is not a template."""
    if not rel.startswith(f"{manifest.runbooks_dir}/"):
        return False
    if str(meta.get("type", "")).strip().casefold() != "runbook":
        return False
    if rel.startswith(f"{manifest.runbooks_dir}/{_TEMPLATES_DIR}/"):
        return False
    tags = meta.get("tags")
    return not (isinstance(tags, list) and any(str(t).strip().casefold() == "template" for t in tags))


def _runbook_findings(rel: str, meta: dict[str, Any], services: tuple[str, frozenset[str]], report: LintReport) -> None:
    rb_id = meta.get("id")
    if not isinstance(rb_id, str) or not _RUNBOOK_ID.fullmatch(rb_id):
        report.errors.append(_finding(E_RUNBOOK_ID_INVALID, rel, "a runbook id looks like RB-NNN"))
    status = str(meta.get("status", "")).strip().casefold()
    if status not in _RUNBOOK_STATUSES:
        report.errors.append(
            _finding(E_RUNBOOK_STATUS_INVALID, rel, "a runbook status is draft, verified or outdated")
        )
    state, names = services
    if state == "ok":
        service = str(meta.get("service", "")).strip().casefold()
        if not service or service not in names:
            report.errors.append(
                _finding(E_RUNBOOK_SERVICE_UNKNOWN, rel, "the service is not a slug or alias of the services map")
            )


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


def lint(root: Path, *, rules: str = RULES_1_2) -> LintReport:
    """Lint the vault at ``root`` without writing anything.

    ``rules`` picks the rule set (``RULES_1_2`` by default, the one
    ``reindex.validation`` counts). Raises
    :class:`~knowledge.errors.KnowledgeConfigError` with ``E_LINT_PATH_NOT_DIR``
    when ``root`` is not a directory and with ``E_LINT_RULES_UNKNOWN`` for an
    unknown rule set.
    """
    if rules not in RULESETS:
        raise KnowledgeConfigError(
            E_LINT_RULES_UNKNOWN, f"vault-lint rules are {', '.join(RULESETS)}; {rules!r} is not one"
        )
    _require_dir(root)
    strict = rules == RULES_1_3

    files = _markdown_files(root)
    report = LintReport(vault=root.resolve().name, files=len(files), rules=rules)
    manifest = _read_manifest(root, report) if strict else _Manifest()
    rels = [path.relative_to(root).as_posix() for path in files]

    link_index = LinkIndex.build(rels)
    attachments: set[str] | None = None
    stem_files: dict[str, list[str]] = {}
    for rel in rels:
        if strict and manifest.excludes(rel):
            continue
        stem_files.setdefault(PurePosixPath(rel).stem.lower(), []).append(rel)
    for stem in sorted(stem_files):
        if len(stem_files[stem]) > 1:
            report.warnings.append(
                {
                    "code": W_WIKILINK_AMBIGUOUS_STEM,
                    "stem": stem,
                    "files": sorted(stem_files[stem]),
                }
            )

    services: tuple[str, frozenset[str]] | None = None
    missing_map_reported = False

    for path, rel in zip(files, rels, strict=True):
        # An ``index.exclude`` match is not held to the frontmatter rules of 1.3
        # (a context package's CLAUDE.md is not a note). The 1.2 rules never read
        # the manifest, so they count the same findings for every file.
        excluded = strict and manifest.excludes(rel)
        try:
            post = frontmatter.load(str(path))
        except Exception as exc:  # noqa: BLE001 - report the class only, never file text
            if not excluded:
                report.errors.append(
                    {
                        "code": E_FRONTMATTER_UNPARSEABLE,
                        "file": rel,
                        "target": "",
                        "message": f"frontmatter does not parse ({type(exc).__name__})",
                    }
                )
            continue
        if not isinstance(post.metadata, dict):
            if not excluded:
                report.errors.append(
                    {
                        "code": E_FRONTMATTER_NOT_MAPPING,
                        "file": rel,
                        "target": "",
                        "message": "frontmatter must be a mapping of keys to values",
                    }
                )
        elif strict:
            if not excluded:
                _frontmatter_findings(rel, post.metadata, manifest, report)
            if _is_runbook(rel, post.metadata, manifest):
                if services is None:
                    services = _load_services(root, manifest.services_file)
                    if services[0] == "invalid":
                        report.errors.append(
                            _finding(
                                E_SERVICES_MAP_INVALID,
                                manifest.services_file,
                                'the services map is not {"services": [{"slug", "aliases"}]}',
                            )
                        )
                if services[0] == "missing" and not missing_map_reported:
                    missing_map_reported = True
                    report.warnings.append(
                        _warning(
                            W_SERVICES_MAP_MISSING,
                            manifest.services_file,
                            "the vault has runbooks and no services map to check their service against",
                        )
                    )
                _runbook_findings(rel, post.metadata, services, report)

        if excluded:
            continue
        body = _strip_code(post.content) if strict else post.content
        seen: set[str] = set()
        for raw in extract_wikilinks(body):
            target = parse_target(raw)
            if target is None or target in seen:
                continue
            seen.add(target)
            if resolve_target(target, link_index, rel) is None:
                if attachments is None:
                    attachments = _attachment_keys(root)
                if _names_attachment(target, attachments):
                    continue
                report.errors.append(
                    {
                        "code": E_WIKILINK_UNRESOLVED,
                        "file": rel,
                        "target": target,
                        "message": f"wikilink [[{target}]] matches no note in the vault",
                    }
                )

    if strict:
        report.errors.extend(_path_findings(rels))

    report.errors.sort(key=lambda e: (e["file"], e["target"], e["code"]))
    report.warnings.sort(key=lambda w: (w["code"], w.get("file") or w.get("stem") or ""))
    return report


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------

_BOUNDARY_LINE = re.compile(r"-{3,}\s*")
_KEY_LINE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_-]*)(?P<sep>[ \t]*:)(?P<value>(?:[ \t][^\r\n]*)?)(?P<nl>\r?\n|)")
_NEWLINE = re.compile(r"[^\n]*\n|[^\n]+")
_UNSAFE = "edit_unsafe"


def _split_lines(text: str) -> list[str]:
    """``text`` as lines that keep their own line ending, so joining them gives ``text`` back."""
    return _NEWLINE.findall(text)


def _frontmatter_end(lines: list[str]) -> int | None:
    """Index of the line that closes the frontmatter, or ``None`` when the text has none."""
    if not lines or _BOUNDARY_LINE.fullmatch(lines[0]) is None:
        return None
    for index in range(1, len(lines)):
        if _BOUNDARY_LINE.fullmatch(lines[index]) is not None:
            return index
    return None


def _find_key(block: list[str], key: str) -> int | None:
    for index, line in enumerate(block):
        match = _KEY_LINE.fullmatch(line)
        if match is not None and match["key"] == key:
            return index
    return None


def _block_end(block: list[str], index: int) -> int:
    """One past the last line that belongs to the key at ``index`` (its indented lines)."""
    end = index + 1
    while end < len(block) and (not block[end].strip() or block[end][0] in " \t"):
        end += 1
    while end > index + 1 and not block[end - 1].strip():
        end -= 1
    return end


def _split_flow_items(body: str) -> list[str]:
    """The comma-separated items of a flow mapping body, respecting quotes and nesting."""
    items: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    escaped = False
    for char in body:
        if quote is not None:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    items.append("".join(current).strip())
    return [item for item in items if item]


Edit = tuple[int, int, list[str]]


def _edits_from_scalar(block: list[str], source: str) -> list[Edit] | None:
    """Edits that move a single-line ``updated`` or ``last_review`` into ``last_updated``."""
    at = _find_key(block, source)
    if at is None:
        return None
    match = _KEY_LINE.fullmatch(block[at])
    assert match is not None
    value = match["value"].strip()
    if not value or value.startswith("#") or _block_end(block, at) != at + 1:
        return None
    existing = _find_key(block, "last_updated")
    if existing is None:
        return [(at, at + 1, [f"last_updated{block[at][len(source):]}"])]
    held = _KEY_LINE.fullmatch(block[existing])
    assert held is not None
    if _block_end(block, existing) != existing + 1:
        return None
    return [(existing, existing + 1, [f"last_updated: {value}{held['nl']}"]), (at, at + 1, [])]


def _edits_from_verified(block: list[str]) -> list[Edit] | None:
    """Edits that lift ``last_verified.date`` into ``last_updated``, keeping the rest as ``verified_against``."""
    at = _find_key(block, "last_verified")
    if at is None or _find_key(block, "verified_against") is not None:
        return None
    match = _KEY_LINE.fullmatch(block[at])
    assert match is not None
    nl = match["nl"] or "\n"
    value = match["value"].strip()

    replacement: list[str]
    end = _block_end(block, at)
    if not value or value.startswith("#"):
        children = block[at + 1 : end]
        indents = [len(c) - len(c.lstrip(" \t")) for c in children if c.strip()]
        if not indents:
            return None
        base = " " * min(indents)
        date_row = re.compile(rf"{base}date[ \t]*:(?P<value>[^\r\n]*)(?:\r?\n|)")
        date_at = next((n for n, child in enumerate(children) if date_row.fullmatch(child)), None)
        if date_at is None:
            return None
        date_match = date_row.fullmatch(children[date_at])
        assert date_match is not None
        date_value = date_match["value"].strip()
        following = children[date_at + 1] if date_at + 1 < len(children) else ""
        if not date_value or date_value.startswith("#") or (
            following.strip() and len(following) - len(following.lstrip(" \t")) > len(base)
        ):
            return None
        rest = children[:date_at] + children[date_at + 1 :]
        replacement = [f"last_updated: {date_value}{nl}"]
        if any(line.strip() for line in rest):
            replacement.append(f"verified_against{block[at][len('last_verified'):]}")
            replacement.extend(rest)
    else:
        if end != at + 1:
            return None
        flow = re.fullmatch(r"\{(?P<body>.*)\}[ \t]*(?P<comment>#.*)?", value)
        if flow is None:
            return None
        body = flow["body"]
        items = _split_flow_items(body)
        date_value = ""
        kept: list[str] = []
        for item in items:
            pair = re.fullmatch(r"(?P<key>[^:]+?)[ \t]*:[ \t]*(?P<value>.*)", item)
            if pair is None:
                return None
            if pair["key"] == "date":
                date_value = pair["value"].strip()
            else:
                kept.append(item)
        if not date_value:
            return None
        replacement = [f"last_updated: {date_value}{nl}"]
        if kept:
            pad = " " if body.startswith(" ") else ""
            comment = f" {flow['comment']}" if flow["comment"] else ""
            replacement.append(f"verified_against: {{{pad}{', '.join(kept)}{pad}}}{comment}{nl}")

    edits: list[Edit] = [(at, end, replacement)]
    existing = _find_key(block, "last_updated")
    if existing is not None:
        if _block_end(block, existing) != existing + 1:
            return None
        edits.append((existing, existing + 1, []))
    return edits


def _apply_edits(block: list[str], edits: list[Edit]) -> list[str]:
    result = list(block)
    for start, end, replacement in sorted(edits, key=lambda edit: edit[0], reverse=True):
        result[start:end] = replacement
    return result


def _plan_date_fix(raw: str, meta: dict[str, Any], content: str) -> tuple[str, str, str] | str:
    """The new text, the source key and the date for a note, or the reason it cannot be fixed."""
    source = next((key for key in ("updated", "last_review") if _is_iso_date(meta.get(key))), None)
    verified = meta.get("last_verified")
    if source is None and isinstance(verified, dict) and _is_iso_date(verified.get("date")):
        source = "last_verified.date"
    if source is None:
        present = any(key in meta for key in ("updated", "last_review")) or isinstance(verified, dict)
        return "source_invalid" if present else "no_source"

    lines = _split_lines(raw)
    end = _frontmatter_end(lines)
    if end is None:
        return _UNSAFE
    block = lines[1:end]
    edits = (
        _edits_from_verified(block) if source == "last_verified.date" else _edits_from_scalar(block, source)
    )
    if edits is None:
        return _UNSAFE
    new_text = "".join(lines[:1] + _apply_edits(block, edits) + lines[end:])

    expected = dict(meta)
    if source == "last_verified.date":
        assert isinstance(verified, dict)
        value = verified["date"]
        rest = {k: v for k, v in verified.items() if k != "date"}
        del expected["last_verified"]
        if rest:
            expected["verified_against"] = rest
    else:
        value = meta[source]
        del expected[source]
    expected["last_updated"] = value
    try:
        reparsed = frontmatter.loads(new_text)
    except Exception:  # noqa: BLE001 - any failure to re-read the edit makes it unsafe
        return _UNSAFE
    if reparsed.metadata != expected or reparsed.content != content:
        return _UNSAFE
    return new_text, source, str(value)


def _write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` atomically, keeping its mode: an interruption leaves the old note whole."""
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def fix(root: Path) -> FixOutcome:
    """Move a date the notes already carry into ``last_updated``, in place.

    For each note with no readable ``last_updated``, the first of ``updated``,
    ``last_review`` and ``last_verified.date`` that holds an ISO date becomes
    ``last_updated``; the rest of a ``last_verified`` mapping stays as
    ``verified_against``. Only frontmatter lines change, and a note is written
    only when re-reading the edited text gives exactly the intended
    frontmatter and the same body. A second run changes nothing. A note that
    is a symbolic link, or resolves outside ``root``, is never written: it
    stays in ``remaining`` as ``edit_unsafe``.

    Which directory this may run on is the caller's to decide: the command
    line refuses a served vault (``E_WRITE_FENCED``) before calling it.
    """
    _require_dir(root)
    outcome = FixOutcome()
    real_root = root.resolve()
    for path in _markdown_files(root):
        rel = path.relative_to(root).as_posix()
        linked = path.is_symlink() or not path.resolve().is_relative_to(real_root)
        try:
            raw = path.read_bytes().decode("utf-8")
            post = frontmatter.loads(raw)
        except Exception:  # noqa: BLE001 - unreadable notes are lint findings, not fix targets
            continue
        meta = post.metadata
        if not isinstance(meta, dict) or _last_updated_problem(meta) is None:
            continue
        planned = _UNSAFE if linked else _plan_date_fix(raw, meta, post.content)
        if isinstance(planned, str):
            outcome.remaining.append({"file": rel, "rule": E_FRONTMATTER_LAST_UPDATED, "reason": planned})
            continue
        new_text, source, value = planned
        _write_text(path, new_text)
        outcome.fixed.append(
            {"file": rel, "rule": E_FRONTMATTER_LAST_UPDATED, "from": source, "to": "last_updated", "value": value}
        )
    return outcome
