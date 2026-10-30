#!/usr/bin/env python3
"""Weekly review-decay report: stale vault documents, grouped by owner.

Reads the committed vault's frontmatter directly. No index and no OpenAI
key — so the same command runs on a laptop and inside a
Bitbucket Pipelines schedule with nothing but the repository checkout.

Thresholds come from ``config.json`` (``review.thresholds_days``) and owner
resolution from ``knowledge.review``, the same module the ``get_stale_docs``
MCP tool uses, so the report and the tool can never disagree.

Usage:
    uv run python scripts/weekly-stale-report.py
    uv run python scripts/weekly-stale-report.py --format markdown --output report.md
    uv run python scripts/weekly-stale-report.py --owner dev-php
    uv run python scripts/weekly-stale-report.py --type runbook --fail-on-stale

Exit codes:
    0  report produced
    1  --fail-on-stale was passed and the vault is not provably clean: at
       least one document is overdue, or at least one carries a date /
       frontmatter / subtree nobody could read. "I could not look" is never
       a pass.
    2  nothing usable to inspect — an unusable vault path, an unknown --owner
       or --type (a filter that matches nobody proves nothing), or a scan that
       read zero curated documents
    3  --fail-on-stale was passed but the thresholds came from nowhere:
       ``config.json`` did not declare ``review.thresholds_days``, so every
       verdict below would be measured against a number nobody chose
    4  a dependency is missing, so the scan never started. Distinct from 1 on
       purpose: an environment that cannot read one document must not share
       its status with a vault that has overdue ones.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _missing_dependency(name: str, exc: BaseException) -> SystemExit:
    """Refuse to start, loudly, instead of exiting like a clean vault.

    An incomplete environment used to surface as a bare ``ImportError``
    traceback, whose exit status is 1 — the very code that means "the vault
    has overdue documents". A gate reading that status cannot tell "I found
    something" from "I never ran", so the missing dependency gets a status of
    its own.
    """
    return SystemExit(
        f"ERROR: falta la dependencia '{name}' ({exc}). "
        "Sin ella el reporte no lee un solo documento: eso no es un vault "
        "limpio, es un entorno incompleto. Instala las dependencias del "
        "repositorio (uv sync) y vuelve a correrlo."
    )


try:
    import frontmatter
except ImportError as exc:
    print(_missing_dependency("python-frontmatter", exc), file=sys.stderr)
    raise SystemExit(4) from exc

try:
    from knowledge import config, errors, review, settings, vault_lint  # noqa: E402
except ImportError as exc:
    print(_missing_dependency("knowledge (src/ del repositorio)", exc), file=sys.stderr)
    raise SystemExit(4) from exc

# Obsidian templates keep unrendered placeholders (``{{date}}``) in their
# frontmatter, which is not valid YAML. They are stubs, not knowledge: the
# indexer already drops them, and the report counts them without listing
# them one by one every week.
_PLACEHOLDER = "{{"

# Compared case-folded: a document saved as ``.MD`` is the same document, and
# a glob anchored to one spelling simply does not see the other.
_MARKDOWN_SUFFIX = ".md"

_FORMATS = ("text", "markdown", "json")


@dataclass
class ScanResult:
    """Everything one pass over the vault produced."""

    documents: list[dict[str, Any]] = field(default_factory=list)
    stale: list[dict[str, Any]] = field(default_factory=list)
    service_owners: dict[str, str] = field(default_factory=dict)
    templates: list[str] = field(default_factory=list)
    unparseable: list[dict[str, str]] = field(default_factory=list)
    invalid_dates: list[dict[str, str]] = field(default_factory=list)
    unwalkable: list[dict[str, str]] = field(default_factory=list)
    known_owners: set[str] = field(default_factory=set)
    known_types: set[str] = field(default_factory=set)
    skipped_non_curated: int = 0
    undated: int = 0


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def _has_placeholder_frontmatter(path: Path) -> bool:
    """True when the file's frontmatter block still holds ``{{...}}`` stubs."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else text[3:2000]
    return _PLACEHOLDER in block


def _rel(vault: Path, path: Path) -> str:
    """Vault-relative POSIX path, or the absolute one when it is outside."""
    try:
        return path.relative_to(vault).as_posix()
    except ValueError:
        return path.as_posix()


def index_excluder(vault: Path) -> tuple[Callable[[str], bool], list[dict[str, str]]]:
    """The predicate for what the vault's ``vault.yaml`` ``index.exclude`` covers, and what stopped it from being read.

    The files it covers are not notes: ``vault-lint`` does not hold them to the
    frontmatter rules, so the report does not put them on a review queue. A
    ``vault.yaml`` nobody can read is an "I could not look" for the same
    reason an unreadable directory is: the exclusion is unknown, so it is
    returned as an ``unwalkable`` entry (which ``--fail-on-stale`` refuses to
    certify) and the scan goes on with nothing excluded.
    """
    try:
        patterns = settings.index_exclude_patterns(settings.read_vault_manifest(vault))
    except (errors.KnowledgeConfigError, OSError) as exc:
        problem = {"path": "vault.yaml", "error": f"{type(exc).__name__}: {str(exc).splitlines()[0]}"}
        return vault_lint.index_exclude_matcher(()), [problem]
    return vault_lint.index_exclude_matcher(patterns), []


def walk_markdown(
    vault: Path, *, index_exclude: Callable[[str], bool] | None = None
) -> tuple[list[Path], list[dict[str, str]]]:
    """Every Markdown file under ``vault``, plus every place the walk failed.

    ``Path.rglob`` answers "here is what I found" and never "here is what I
    could not open", which makes three ordinary vault shapes vanish without
    a trace:

    - a subdirectory reached through a symlink — Python 3.13+ walks with
      ``recurse_symlinks=False``, and an Obsidian vault with a linked folder
      is routine, not exotic;
    - a directory this process cannot read;
    - a file whose suffix is not lowercase (``Runbook.MD``), because a glob
      anchored to one spelling does not see the other.

    Each of those means the scan inspected LESS than it believes. They are
    returned as ``unwalkable`` entries so the caller can refuse to certify a
    vault over a subtree it never opened. Symlinked directories are followed
    on purpose, with a device/inode set as the loop guard so a cycle ends the
    walk instead of hanging it.

    Directories the corpus contract already excludes (dot-directories, the
    memory export folder, what ``index_exclude`` covers) are not reported when
    they fail to open: nothing inside them would have been reviewed anyway.
    """
    files: list[Path] = []
    unwalkable: list[dict[str, str]] = []
    seen: set[tuple[int, int]] = set()
    pending: list[Path] = [vault]

    def _unreachable(path: Path, exc: OSError) -> None:
        rel = _rel(vault, path)
        if rel and rel != "." and not review.is_curated(rel, index_exclude=index_exclude):
            return
        unwalkable.append(
            {"path": rel or ".", "error": f"{type(exc).__name__}: {exc.strerror or exc}"}
        )

    while pending:
        current = pending.pop()
        try:
            info = current.stat()
        except OSError as exc:
            _unreachable(current, exc)
            continue
        ident = (info.st_dev, info.st_ino)
        if ident in seen:
            continue
        seen.add(ident)
        try:
            entries = sorted(current.iterdir())
        except OSError as exc:
            _unreachable(current, exc)
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError as exc:
                _unreachable(entry, exc)
                continue
            if is_dir:
                pending.append(entry)
            elif entry.suffix.lower() == _MARKDOWN_SUFFIX:
                files.append(entry)

    return sorted(files), sorted(unwalkable, key=lambda item: item["path"])


def scan_vault(
    vault: Path, *, today: date, doc_type: str | None = None
) -> ScanResult:
    """Parse every curated vault document and flag the overdue ones."""
    result = ScanResult()

    excluded, manifest_problems = index_excluder(vault)
    paths, result.unwalkable = walk_markdown(vault, index_exclude=excluded)
    result.unwalkable = sorted([*result.unwalkable, *manifest_problems], key=lambda item: item["path"])
    for path in paths:
        rel = _rel(vault, path)
        if not review.is_curated(rel, index_exclude=excluded):
            result.skipped_non_curated += 1
            continue
        try:
            meta = dict(frontmatter.load(str(path)).metadata)
        except Exception as exc:  # noqa: BLE001 - any YAML error means "not indexable"
            if _has_placeholder_frontmatter(path):
                result.templates.append(rel)
            else:
                result.unparseable.append(
                    {"file_path": rel, "error": str(exc).strip().splitlines()[0]}
                )
            continue
        result.documents.append({"file_path": rel, "meta": meta})

    result.service_owners = review.build_service_owner_map(
        doc["meta"] for doc in result.documents
    )
    # Computed over the whole corpus, not over the filtered slice: an owner
    # with nothing overdue is still a real owner, and `--owner` has to be
    # able to tell that apart from a typo.
    result.known_owners = review.known_owners(
        (doc["meta"] for doc in result.documents), result.service_owners
    )
    result.known_types = {
        str(doc["meta"].get("type", "")).strip()
        for doc in result.documents
        if str(doc["meta"].get("type", "")).strip()
    }

    for doc in result.documents:
        meta = doc["meta"]
        if doc_type and str(meta.get("type", "")) != doc_type:
            continue
        date_state, raw_date = review.last_updated_state(meta)
        if date_state == review.LAST_UPDATED_MISSING:
            result.undated += 1
            continue
        if date_state == review.LAST_UPDATED_INVALID:
            # Not "no data": data that could not be read. Reported by name so
            # somebody can repair the frontmatter instead of the document
            # staying invisible to every review route forever.
            owner, owner_source = review.resolve_owner(meta, result.service_owners)
            result.invalid_dates.append(
                {
                    "file_path": doc["file_path"],
                    "last_updated": raw_date,
                    "type": str(meta.get("type", "")),
                    "service": str(meta.get("service", "")),
                    "owner": owner,
                    "owner_source": owner_source,
                }
            )
            continue
        annotation = review.review_status(meta, today=today)
        if not annotation:
            continue
        owner, owner_source = review.resolve_owner(meta, result.service_owners)
        result.stale.append(
            {
                "file_path": doc["file_path"],
                "title": str(meta.get("title", "") or Path(doc["file_path"]).stem),
                "type": str(meta.get("type", "")),
                "service": str(meta.get("service", "")),
                "owner": owner,
                "owner_source": owner_source,
                "last_updated": str(meta.get("last_updated", "")),
                "last_verified": str(meta.get("last_verified", "")),
                "threshold_days": review.threshold_for(meta.get("type")),
                "reason": review.REASON_OVERDUE,
                **annotation,
            }
        )

    result.stale.sort(key=lambda d: (-int(d["age_days"]), d["file_path"]))
    result.invalid_dates.sort(key=lambda d: d["file_path"])
    return result


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def build_payload(
    result: ScanResult, *, today: date, vault: Path, owner_filter: str | None
) -> dict[str, Any]:
    """Machine-readable body of the report; every renderer works off this.

    ``totals.stale`` and ``totals.unassigned`` count what the report shows,
    so they respect ``owner_filter``; the corpus counters describe the whole
    scan regardless of the filter.

    ``unreadable`` is the union of the three ways knowledge can escape the
    review rules: frontmatter no YAML parser accepts, a ``last_updated`` that
    is present but unreadable, and a subtree the walk could not open at all.
    It is the number ``--fail-on-stale`` consults alongside ``stale``, because
    an unknown age is not a clean age and an unopened folder is not an empty
    one.

    ``thresholds_from_config`` carries the provenance of every limit in the
    report. A missing ``config.json`` yields a plausible-looking 270-day
    catch-all that no human ever chose, and the numbers alone cannot tell
    that apart from a declared one.
    """
    stale = [d for d in result.stale if not owner_filter or d["owner"] == owner_filter]
    invalid = [
        d for d in result.invalid_dates if not owner_filter or d["owner"] == owner_filter
    ]
    groups = review.group_by_owner(stale)
    return {
        "generated_on": today.isoformat(),
        "vault_path": str(vault),
        "thresholds_days": dict(config.REVIEW_THRESHOLDS_DAYS),
        "thresholds_from_config": review.thresholds_are_configured(),
        "owner_filter": owner_filter or "",
        "totals": {
            "documents_scanned": len(result.documents),
            "stale": len(stale),
            "unassigned": sum(1 for d in stale if d["owner"] == review.UNASSIGNED),
            "without_last_updated": result.undated,
            "templates_skipped": len(result.templates),
            "non_curated_skipped": result.skipped_non_curated,
            "unparseable": len(result.unparseable),
            "invalid_last_updated": len(invalid),
            # Never filtered by owner: a folder nobody could open may well
            # have held the documents of the owner being asked about.
            "unwalkable": len(result.unwalkable),
            "unreadable": len(result.unparseable) + len(invalid) + len(result.unwalkable),
        },
        "owners": [
            {"owner": owner, "count": len(docs), "documents": docs} for owner, docs in groups
        ],
        "unparseable": result.unparseable,
        "invalid_last_updated": invalid,
        "unwalkable": result.unwalkable,
    }


def _line(doc: dict[str, Any]) -> str:
    inherited = " (heredado del hub)" if doc["owner_source"] == review.OWNER_FROM_SERVICE_HUB else ""
    detail = " · ".join(
        part
        for part in (
            doc["type"],
            doc["service"],
            f"last_updated {doc['last_updated']}",
        )
        if part
    )
    return (
        f"  {doc['age_days']:>4}d (límite {doc['threshold_days']}d)  {doc['file_path']}\n"
        f"        {detail}{inherited}"
    )


def _thresholds_provenance(payload: dict[str, Any]) -> str:
    """One line saying whether a human chose the limits this report applied."""
    if payload["thresholds_from_config"]:
        return "config.json (review.thresholds_days)"
    return (
        "SIN CONFIGURAR — config.json no declara review.thresholds_days; "
        "los límites de abajo son el respaldo genérico, no una decisión del equipo"
    )


def render_text(payload: dict[str, Any]) -> str:
    t = payload["totals"]
    out = [
        "Reporte semanal de documentos vencidos del vault",
        f"Fecha: {payload['generated_on']}",
        (
            f"Revisados: {t['documents_scanned']} · Vencidos: {t['stale']} · "
            f"Sin dueño: {t['unassigned']} · Sin last_updated: {t['without_last_updated']} · "
            f"Ilegibles: {t['unreadable']} · Sin recorrer: {t['unwalkable']}"
        ),
        f"Umbrales: {_thresholds_provenance(payload)}",
    ]
    if payload["owner_filter"]:
        out.append(f"Filtrado por dueño: {payload['owner_filter']}")
    out.append("")

    if not payload["owners"]:
        out.append("Sin documentos vencidos. Nada que revalidar esta semana.")
    for group in payload["owners"]:
        noun = "documento vencido" if group["count"] == 1 else "documentos vencidos"
        out.append(f"{group['owner']} — {group['count']} {noun}")
        out.extend(_line(doc) for doc in group["documents"])
        out.append("")

    if payload["unparseable"]:
        out.append("Frontmatter ilegible (invisible para el índice; hay que repararlo):")
        out.extend(f"  {item['file_path']} — {item['error']}" for item in payload["unparseable"])
        out.append("")

    if payload["invalid_last_updated"]:
        out.append(
            "last_updated ilegible (no se puede calcular su antigüedad; hay que repararlo):"
        )
        out.extend(
            f"  {item['file_path']} — last_updated: {item['last_updated']} · dueño {item['owner']}"
            for item in payload["invalid_last_updated"]
        )
        out.append("")

    if payload["unwalkable"]:
        out.append(
            "Rutas sin recorrer (el reporte NO vio lo que hay dentro; hay que darles acceso):"
        )
        out.extend(f"  {item['path']} — {item['error']}" for item in payload["unwalkable"])
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render_markdown(payload: dict[str, Any]) -> str:
    t = payload["totals"]
    out = [
        "# Documentos vencidos del vault",
        "",
        f"**Fecha:** {payload['generated_on']}  ",
        (
            f"**Revisados:** {t['documents_scanned']} · **Vencidos:** {t['stale']} · "
            f"**Sin dueño:** {t['unassigned']} · **Ilegibles:** {t['unreadable']} · "
            f"**Sin recorrer:** {t['unwalkable']}"
        ),
        f"**Umbrales:** {_thresholds_provenance(payload)}",
        "",
    ]
    if payload["owner_filter"]:
        out += [f"Filtrado por dueño: `{payload['owner_filter']}`", ""]

    if not payload["owners"]:
        out += ["Sin documentos vencidos. Nada que revalidar esta semana.", ""]

    for group in payload["owners"]:
        out += [
            f"## {group['owner']} ({group['count']})",
            "",
            "| Antigüedad | Límite | Documento | Tipo | Servicio | last_updated | Dueño vía |",
            "|---|---|---|---|---|---|---|",
        ]
        out += [
            (
                f"| {d['age_days']}d | {d['threshold_days']}d | `{d['file_path']}` | "
                f"{d['type']} | {d['service']} | {d['last_updated']} | {d['owner_source']} |"
            )
            for d in group["documents"]
        ]
        out.append("")

    if payload["unparseable"]:
        out += ["## Frontmatter ilegible", ""]
        out += [f"- `{i['file_path']}` — {i['error']}" for i in payload["unparseable"]]
        out.append("")

    if payload["invalid_last_updated"]:
        out += ["## last_updated ilegible", ""]
        out += [
            f"- `{i['file_path']}` — `last_updated: {i['last_updated']}` · dueño {i['owner']}"
            for i in payload["invalid_last_updated"]
        ]
        out.append("")

    if payload["unwalkable"]:
        out += ["## Rutas sin recorrer", ""]
        out += [f"- `{i['path']}` — {i['error']}" for i in payload["unwalkable"]]
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render(payload: dict[str, Any], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if fmt == "markdown":
        return render_markdown(payload)
    return render_text(payload)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="weekly-stale-report.py",
        description="Reporte de documentos del vault vencidos, agrupados por dueño.",
    )
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="Vault a analizar (default: VAULT_PATH o el vault por defecto de vaults.yaml)",
    )
    parser.add_argument("--format", choices=_FORMATS, default="text", help="Formato de salida")
    parser.add_argument(
        "--owner",
        default=None,
        help="Mostrar solo los documentos de este dueño (rechaza un dueño desconocido)",
    )
    parser.add_argument(
        "--type",
        dest="doc_type",
        default=None,
        help="Limitar a un `type` de frontmatter (rechaza un tipo que no existe en el vault)",
    )
    parser.add_argument("--output", type=Path, default=None, help="Escribir el reporte a un archivo")
    parser.add_argument(
        "--today",
        default=None,
        help="Fecha de referencia YYYY-MM-DD (default: hoy). Hace el reporte reproducible.",
    )
    parser.add_argument(
        "--fail-on-stale",
        action="store_true",
        help=(
            "Salir con código 1 si hay al menos un documento vencido o ilegible. "
            "Un documento cuya antigüedad no se puede leer nunca cuenta como limpio."
        ),
    )
    return parser.parse_args(argv)


def _reject_unknown_filter(
    label: str, value: str, known: set[str], *, hint: str
) -> None:
    """Print why a filter was rejected, naming what would have been accepted."""
    known_list = ", ".join(sorted(known)) if known else "(ninguno)"
    print(
        f"ERROR: {label} '{value}' no existe en el vault. {hint}\n"
        f"       Valores conocidos: {known_list}",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.vault is not None:
        vault = args.vault
    else:
        # The vault lives in its own repository: without --vault it is the one
        # VAULT_PATH (or the default vault of vaults.yaml) declares, and with
        # neither there is nothing to read, which is not a clean vault.
        try:
            vault = config.VAULT_PATH
        except errors.KnowledgeConfigError as exc:
            print(f"ERROR: {exc}. Pasa --vault con un clon del repositorio del vault.", file=sys.stderr)
            return 2
    if not vault.is_dir():
        print(f"ERROR: el vault no existe o no es un directorio: {vault}", file=sys.stderr)
        return 2

    try:
        today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()
    except ValueError:
        print(f"ERROR: --today debe ser YYYY-MM-DD, se recibió: {args.today}", file=sys.stderr)
        return 2

    result = scan_vault(vault, today=today, doc_type=args.doc_type)

    # A filter that matches nobody has not proven that nobody is overdue; it
    # has proven the filter is wrong. Rejecting it here is what keeps
    # `--owner <typo> --fail-on-stale` from painting the gate green.
    if args.doc_type and args.doc_type not in result.known_types:
        _reject_unknown_filter(
            "--type",
            args.doc_type,
            result.known_types,
            hint="Un tipo inexistente filtraría todo y dejaría el reporte vacío.",
        )
        return 2
    if args.owner and args.owner not in result.known_owners:
        _reject_unknown_filter(
            "--owner",
            args.owner,
            result.known_owners,
            hint="Un dueño mal escrito filtraría todo y dejaría el reporte vacío.",
        )
        return 2

    payload = build_payload(result, today=today, vault=vault, owner_filter=args.owner)
    text = render(payload, args.format)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"Reporte escrito en {args.output}")
    else:
        sys.stdout.write(text)

    if args.fail_on_stale:
        totals = payload["totals"]
        # "No encontré nada" only counts as a pass when there was something to
        # look at. A scan that read zero curated documents is a wrong path or
        # an empty checkout, not a clean vault.
        if not totals["documents_scanned"]:
            print(
                f"ERROR: no se leyó ningún documento curado en {vault}; "
                "no hay nada que validar.",
                file=sys.stderr,
            )
            return 2
        # Checked before the verdict, and with its own code: when nobody
        # declared the limits, every "vencido / no vencido" below was measured
        # against a number this program invented. `threshold_for` can never
        # refuse to answer, so the provenance is the only thing that separates
        # a configured 90-day runbook from the 270-day catch-all that silently
        # replaces it.
        if not payload["thresholds_from_config"]:
            print(
                "ERROR: config.json no declara review.thresholds_days; los "
                "umbrales aplicados son un respaldo genérico que nadie eligió. "
                "Un veredicto medido contra números inventados no es un PASS.",
                file=sys.stderr,
            )
            return 3
        if totals["stale"] or totals["unreadable"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
