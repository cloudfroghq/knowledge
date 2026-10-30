#!/usr/bin/env python3
"""Promotion bridge: a pinned memory observation becomes a curated vault document.

This is the opposite direction from ``export_memories.py``. That script dumps
memories into ``90 - Memory/`` as second-class notes fenced off from search;
this one takes a memory that earned it — pinned, and of an allowlisted type
(``decision`` or ``discovery``) — and turns it into a document a human owns.

The vault is human-curated and its only writer is a merged pull request, so
this bridge never writes inside ``VAULT_PATH``. It renders the candidate
documents into an output directory outside the vault, together with a git
patch that adds them, and stops there.

Two subcommands, in the order they must run:

``plan``
    Asks memory which pinned observations are eligible, renders one candidate
    document per observation, and writes the promotion package: the documents,
    a ``git apply``-able patch, a manifest, and the pull-request description.
    Nothing is written to the vault and nothing is stamped in memory.

``stamp``
    Runs *after* the pull request is merged. For every entry in the manifest it
    checks that the document actually exists in the vault, and only then asks
    memory to record the ``knowledge_ref`` on the observation. Stamping before
    the merge would point a reference at a document no checkout has, which is
    exactly what ``memory doctor --check knowledge_ref_dangling`` reports as a
    defect.

Usage:
    uv run python scripts/promote-memories.py plan --project nextcloud
    uv run python scripts/promote-memories.py plan --project nextcloud --types decision --limit 5
    uv run python scripts/promote-memories.py stamp --manifest build/promotions/<run>/manifest.json
    uv run python scripts/promote-memories.py stamp --manifest ... --dry-run

Exit codes (shared by both subcommands):
    0  the run completed and its output is trustworthy
    2  nothing usable to inspect — zero pinned observations in scope, an
       output directory that would land inside the vault, an unusable vault
       path, a manifest with no entries, or a filter that names nothing
       promotable. Zero inspected units is never a pass.
    3  the run is partial and a human has to act: a candidate collides with a
       document that already exists in the vault (``plan``), or a document is
       still missing from the vault because the pull request is not merged yet
       (``stamp``).
    4  the environment could not run the bridge at all — a missing dependency,
       a missing memory binary, or a memory call that failed. Distinct from 1
       and 3 on purpose: "I never ran" must not share a status with "I ran and
       found something".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

EXIT_OK = 0
EXIT_NOTHING_TO_INSPECT = 2
EXIT_PARTIAL = 3
EXIT_ENVIRONMENT = 4


def _missing_dependency(name: str, exc: BaseException) -> str:
    """Refuse to start, loudly, instead of exiting like a clean run.

    An incomplete environment used to surface as a bare ``ImportError``
    traceback, whose exit status is 1. A caller reading that status cannot
    tell "I found nothing to promote" from "I never started", so the missing
    dependency gets a status of its own.
    """
    return (
        f"ERROR: falta la dependencia '{name}' ({exc}). Sin ella el bridge no lee "
        "una sola observación: eso no es memory sin candidatos, es un entorno "
        "incompleto. Instala las dependencias del repositorio (uv sync) y vuelve "
        "a correrlo."
    )


try:
    import frontmatter
except ImportError as exc:  # pragma: no cover - exercised by the environment, not the suite
    print(_missing_dependency("python-frontmatter", exc), file=sys.stderr)
    raise SystemExit(EXIT_ENVIRONMENT) from exc

try:
    from knowledge import config  # noqa: E402
except ImportError as exc:  # pragma: no cover - same
    print(_missing_dependency("knowledge (src/ del repositorio)", exc), file=sys.stderr)
    raise SystemExit(EXIT_ENVIRONMENT) from exc


# Characters that cannot appear in a vault filename. The forward slash would
# silently create a folder, and the rest either break Obsidian wikilinks
# (``#``, ``|``, ``[``, ``]``, ``^``) or are illegal on one of the platforms
# the vault is cloned onto.
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]+')
_MAX_FILENAME_STEM = 80

# The memory default binary, matching the path the memory stack is installed
# at. Overridable, because a checkout that builds its own binary must be able
# to test against it.
_DEFAULT_MEMORY_BIN = Path.home() / "Projects" / "memory" / "bin" / "memory"


class BridgeError(Exception):
    """A failure that maps to one of the documented exit codes."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# memory error codes that mean "the caller asked for something impossible",
# not "the environment is broken". They keep a mistyped ``--types`` from
# sharing an exit status with a missing binary: one is fixed by retyping the
# command, the other by fixing the machine.
_CALLER_ERROR_CODES = frozenset({"type_not_promotable", "unknown_project", "invalid_enum", "missing_field"})


# ---------------------------------------------------------------------------
# memory plumbing
# ---------------------------------------------------------------------------


def run_memory(memory_bin: Path, args: list[str], *, timeout: int = 120) -> dict[str, Any]:
    """Run one memory command with ``--json`` and return its parsed envelope.

    Every failure is raised, never swallowed. A non-zero exit whose stdout
    holds memory's ``{"error","code"}`` envelope keeps the ``code`` in the
    message, because that string is what a caller branches on.
    """
    cmd = [str(memory_bin), *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise BridgeError(
            f"no se encontró el binario de memory en {memory_bin} ({exc}). "
            "Pásalo con --memory-bin o compílalo con: go build -o bin/memory ./cmd/memory",
            EXIT_ENVIRONMENT,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"memory no respondió en {timeout}s: {' '.join(cmd)} ({exc})", EXIT_ENVIRONMENT) from exc

    stdout = proc.stdout or ""
    envelope: dict[str, Any] | None = None
    if stdout.strip():
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                envelope = parsed
        except ValueError:
            envelope = None

    if proc.returncode != 0:
        detail = ""
        exit_code = EXIT_ENVIRONMENT
        memory_code = str(envelope.get("code", "")) if envelope else ""
        if memory_code:
            detail = f" [code={memory_code}] {envelope.get('error', '')}".rstrip()
            if memory_code in _CALLER_ERROR_CODES:
                exit_code = EXIT_NOTHING_TO_INSPECT
        elif proc.stderr.strip():
            detail = " " + proc.stderr.strip().splitlines()[0]
        raise BridgeError(
            f"memory salió con código {proc.returncode}: {' '.join(cmd)}.{detail}",
            exit_code,
        )

    if envelope is None:
        raise BridgeError(
            f"memory devolvió una salida que no es un objeto JSON: {' '.join(cmd)}",
            EXIT_ENVIRONMENT,
        )
    return envelope


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def safe_filename(title: str, fallback: str) -> str:
    """Turn an observation title into a vault filename stem.

    Vault filenames keep spaces and capitalisation (``Push System.md``), so
    this is not a slugifier: it removes what a path cannot hold and leaves the
    rest alone. An empty result falls back to the observation's sync id, which
    is never empty for a promotable observation.
    """
    stem = _ILLEGAL_FILENAME_CHARS.sub(" ", str(title or ""))
    stem = re.sub(r"\s+", " ", stem).strip().strip(".")
    if len(stem) > _MAX_FILENAME_STEM:
        stem = stem[:_MAX_FILENAME_STEM].rstrip()
    return stem or fallback


def target_folder(obs_type: str) -> str:
    """Vault folder for an observation type, from ``config.json``.

    A type with no configured folder is an error and not a default: dropping a
    promoted document into a plausible-looking folder is how a vault grows a
    taxonomy nobody agreed to.
    """
    folder = str(config.PROMOTION_TARGET_FOLDERS.get(obs_type, "")).strip().strip("/")
    if not folder:
        raise BridgeError(
            f"el tipo de observación {obs_type!r} no tiene carpeta destino en "
            "config.json (memory.promotion.target_folders). Declárala antes de promover.",
            EXIT_ENVIRONMENT,
        )
    return folder


def render_document(candidate: dict[str, Any], *, today: str) -> str:
    """Render one candidate as a curated vault document.

    The body carries the observation verbatim. Everything the reviewer needs
    to judge it — where it came from, which ticket, which runbook, which graph
    commit — travels in the frontmatter, so it survives indexing and can be
    queried with ``search_by_metadata``.
    """
    obs_type = str(candidate["type"])
    meta: dict[str, Any] = {
        # `decision` and `discovery` are new document types in the vault. The
        # alternative was to relabel them as `architecture` or `reference`,
        # which would claim a human wrote them up as such. They fall through
        # to the `default` review threshold, which is the right cadence for a
        # fact nobody has revalidated yet.
        "type": obs_type,
        "status": config.PROMOTION_DOC_STATUS,
        "owner": config.PROMOTION_DEFAULT_OWNER,
        "last_updated": today,
        "tags": ["memory-promotion", obs_type],
        # `source` and `promoted_from` are the provenance pair: the first says
        # a machine rendered this, the second says which memory it rendered,
        # so the backlink survives even if the observation is later edited.
        "source": "memory-promotion",
        "promoted_from": str(candidate["sync_id"]),
        "memory_project": str(candidate.get("project", "")),
        "memory_type": obs_type,
        "memory_created_at": str(candidate.get("created_at", "")),
    }
    for key, field_name in (("jira_key", "jira_key"), ("runbook_id", "runbook_id"), ("graph_commit", "graph_commit")):
        value = str(candidate.get(key, "") or "").strip()
        if value:
            meta[field_name] = value

    title = str(candidate.get("title") or candidate["sync_id"])
    body_lines = [
        f"# {title}",
        "",
        "> Documento candidato generado por el bridge de promoción de memory. Su",
        f"> `status` es `{config.PROMOTION_DOC_STATUS}` hasta que alguien lo revise en el pull request:",
        "> revisa el contenido, corrige lo que haga falta y sube el `status` antes de",
        "> aprobarlo. Al fusionarlo, corre `promote-memories.py stamp` para que la",
        "> observación de origen apunte a este documento.",
        "",
        "## Contenido",
        "",
        str(candidate.get("content", "")).strip(),
        "",
        "## Procedencia",
        "",
        "| Campo | Valor |",
        "| --- | --- |",
        f"| Observación | `{candidate['sync_id']}` (#{candidate['observation_id']}) |",
        f"| Tipo | `{obs_type}` |",
        f"| Proyecto | `{candidate.get('project', '')}` |",
        f"| Creada | {candidate.get('created_at', '')} |",
    ]
    if str(candidate.get("topic_key", "") or "").strip():
        body_lines.append(f"| Topic key | `{candidate['topic_key']}` |")
    if str(candidate.get("jira_key", "") or "").strip():
        body_lines.append(f"| Ticket | {candidate['jira_key']} |")
    if str(candidate.get("runbook_id", "") or "").strip():
        body_lines.append(f"| Runbook | {candidate['runbook_id']} |")
    if str(candidate.get("graph_commit", "") or "").strip():
        body_lines.append(f"| Graph commit | `{candidate['graph_commit']}` |")
    body_lines.append("")

    post = frontmatter.Post("\n".join(body_lines), **meta)
    return frontmatter.dumps(post) + "\n"


# ---------------------------------------------------------------------------
# Patch construction
# ---------------------------------------------------------------------------


def git_blob_sha1(text: str) -> str:
    """Git's object id for a blob, so the emitted patch carries a real index line."""
    data = text.encode("utf-8")
    header = f"blob {len(data)}\0".encode()
    # Not a cryptographic use: this reproduces git's own blob object-id format
    # (sha1(header + data)), which callers rely on to match `git hash-object`
    # and `git apply`. Any other algorithm produces an id git does not recognize.
    # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
    return hashlib.sha1(header + data).hexdigest()  # noqa: S324 - git's object id is sha1 by definition


def new_file_patch(repo_rel_path: str, content: str) -> str:
    """Build a ``git apply``-able patch hunk that creates one file.

    Only the create case is emitted. A promotion that would modify an existing
    curated document is refused upstream, so this never has to produce a
    context diff — and never has to guess what the current content is.
    """
    lines = content.split("\n")
    # A trailing newline leaves an empty final element that is not a line.
    if lines and lines[-1] == "":
        lines.pop()
    blob = git_blob_sha1(content)
    out = [
        f"diff --git a/{repo_rel_path} b/{repo_rel_path}",
        "new file mode 100644",
        f"index 0000000..{blob[:7]}",
        "--- /dev/null",
        f"+++ b/{repo_rel_path}",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    out.extend("+" + line for line in lines)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@dataclass
class PlanEntry:
    observation_id: int
    sync_id: str
    obs_type: str
    title: str
    knowledge_ref: str
    repo_path: str
    document_file: str
    sha256: str


@dataclass
class PlanResult:
    run_id: str
    project: str
    scan: dict[str, Any]
    entries: list[PlanEntry] = field(default_factory=list)
    collisions: list[dict[str, str]] = field(default_factory=list)
    output_dir: Path | None = None


def vault_repo_prefix() -> str:
    """Path of the vault root inside the git repository that holds it.

    A vault lives in its own repository,
    so the patch is relative to that repository's root: ``""`` when the vault
    is the repository root, ``docs/vault`` when it sits in a subdirectory. The
    repository is the nearest ancestor of ``config.VAULT_PATH`` that holds a
    ``.git`` entry (a directory, or the file of a worktree or submodule).
    """
    vault = Path(config.VAULT_PATH).resolve()
    for candidate in (vault, *vault.parents):
        if (candidate / ".git").exists():
            prefix = PurePosixPath(vault.relative_to(candidate))
            return "" if str(prefix) == "." else str(prefix)
    raise BridgeError(
        f"el vault ({vault}) no está dentro de un repositorio git; el bridge no "
        "puede construir un patch para él. Apunta VAULT_PATH a un clon del "
        "repositorio del vault.",
        EXIT_NOTHING_TO_INSPECT,
    )


def assert_output_is_outside_vault(output_root: Path) -> None:
    """The vault is read-only for this bridge, and that is enforced, not assumed.

    A misconfigured ``output_dir`` is the one mistake that would turn a
    candidate generator into a vault writer, so it is checked on resolved
    paths — a symlink or a ``..`` in the configured value cannot get past it.
    """
    vault = Path(config.VAULT_PATH).resolve()
    resolved = output_root.resolve()
    if resolved == vault or resolved.is_relative_to(vault):
        raise BridgeError(
            f"el directorio de salida ({resolved}) está dentro del vault ({vault}). "
            "El vault es de solo lectura para este bridge: su único escritor es un "
            "pull request fusionado.",
            EXIT_NOTHING_TO_INSPECT,
        )


def build_plan(
    *,
    memory_bin: Path,
    project: str,
    types: list[str],
    limit: int,
    output_root: Path,
    run_id: str,
    today: str,
    dry_run: bool,
) -> PlanResult:
    assert_output_is_outside_vault(output_root)

    vault = Path(config.VAULT_PATH)
    if not vault.is_dir():
        raise BridgeError(
            f"VAULT_PATH no existe o no es un directorio: {vault}. Sin él no se puede "
            "comprobar si un candidato chocaría con un documento ya curado.",
            EXIT_NOTHING_TO_INSPECT,
        )

    args = ["project", project, "promote", "list", "--json"]
    if types:
        args += ["--types", ",".join(types)]
    if limit > 0:
        args += ["--limit", str(limit)]
    envelope = run_memory(memory_bin, args)

    scan = envelope.get("result")
    if not isinstance(scan, dict):
        raise BridgeError("la respuesta de `memory project promote list` no trae `result`", EXIT_ENVIRONMENT)

    result = PlanResult(run_id=run_id, project=project, scan=scan)

    if int(scan.get("pinned_inspected", 0)) == 0:
        raise BridgeError(
            f"no hay observaciones pinneadas en el proyecto {project!r}: cero unidades "
            "inspeccionadas no es un proyecto sin candidatos, es un proyecto sin nada "
            "que juzgar. Pinnea (mem_pin) lo que valga la pena promover.",
            EXIT_NOTHING_TO_INSPECT,
        )

    candidates = scan.get("candidates") or []
    prefix = vault_repo_prefix()
    used_paths: set[str] = set()

    for candidate in candidates:
        obs_type = str(candidate["type"])
        folder = target_folder(obs_type)
        stem = safe_filename(candidate.get("title", ""), str(candidate["sync_id"]))
        vault_rel = f"{folder}/{stem}.md"
        # Two pinned observations can carry the same title. Disambiguating by
        # sync id keeps both, instead of one silently overwriting the other in
        # the patch.
        if vault_rel in used_paths:
            vault_rel = f"{folder}/{stem} ({candidate['sync_id']}).md"
        used_paths.add(vault_rel)

        if (vault / vault_rel).exists():
            result.collisions.append(
                {
                    "sync_id": str(candidate["sync_id"]),
                    "vault_path": vault_rel,
                    "reason": "ya existe un documento curado en esa ruta",
                }
            )
            continue

        content = render_document(candidate, today=today)
        result.entries.append(
            PlanEntry(
                observation_id=int(candidate["observation_id"]),
                sync_id=str(candidate["sync_id"]),
                obs_type=obs_type,
                title=str(candidate.get("title", "")),
                knowledge_ref=vault_rel,
                repo_path=f"{prefix}/{vault_rel}" if prefix else vault_rel,
                document_file=f"documents/{vault_rel}",
                sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        )
        if not dry_run:
            _write_document(output_root / run_id, vault_rel, content)

    if not dry_run:
        result.output_dir = output_root / run_id
        _write_package(result, today=today, memory_bin=memory_bin, types=types)

    return result


def _write_document(run_dir: Path, vault_rel: str, content: str) -> None:
    target = run_dir / "documents" / vault_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _write_package(result: PlanResult, *, today: str, memory_bin: Path, types: list[str]) -> None:
    run_dir = result.output_dir
    assert run_dir is not None
    run_dir.mkdir(parents=True, exist_ok=True)

    patch_parts = []
    for entry in result.entries:
        content = (run_dir / "documents" / entry.knowledge_ref).read_text(encoding="utf-8")
        patch_parts.append(new_file_patch(entry.repo_path, content))
    (run_dir / "promotion.patch").write_text("".join(patch_parts), encoding="utf-8")

    branch = f"{config.PROMOTION_BRANCH_PREFIX}-{result.run_id}"
    manifest = {
        "run_id": result.run_id,
        "generated_at": today,
        "project": result.project,
        "memory_bin": str(memory_bin),
        "types_requested": types,
        "types_applied": result.scan.get("types", []),
        "branch": branch,
        "vault_path": str(config.VAULT_PATH),
        "config_from_repo": config.PROMOTION_FROM_CONFIG,
        "scan": {
            "pinned_inspected": result.scan.get("pinned_inspected", 0),
            "type_excluded": result.scan.get("type_excluded", 0),
            "already_promoted": result.scan.get("already_promoted", 0),
            "not_syncable": result.scan.get("not_syncable", 0),
            "no_project": result.scan.get("no_project", 0),
            "candidates": len(result.scan.get("candidates") or []),
        },
        "collisions": result.collisions,
        "entries": [
            {
                "observation_id": e.observation_id,
                "sync_id": e.sync_id,
                "type": e.obs_type,
                "title": e.title,
                "knowledge_ref": e.knowledge_ref,
                "repo_path": e.repo_path,
                "document_file": e.document_file,
                "sha256": e.sha256,
            }
            for e in result.entries
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "PR.md").write_text(_render_pr_body(result, manifest), encoding="utf-8")


def _render_pr_body(result: PlanResult, manifest: dict[str, Any]) -> str:
    scan = manifest["scan"]
    lines = [
        f"# Promoción de memoria a conocimiento curado — {result.project}",
        "",
        f"Corrida `{result.run_id}`. Este pull request agrega "
        f"{len(result.entries)} documento(s) al vault a partir de observaciones "
        "pinneadas de memory.",
        "",
        "## Qué se inspeccionó",
        "",
        "| Métrica | Valor |",
        "| --- | --- |",
        f"| Observaciones pinneadas inspeccionadas | {scan['pinned_inspected']} |",
        f"| Fuera de la allowlist ({', '.join(manifest['types_applied'])}) | {scan['type_excluded']} |",
        f"| Ya promovidas (con `knowledge_ref`) | {scan['already_promoted']} |",
        f"| Sin `sync_id` (no pueden llevar la marca) | {scan['not_syncable']} |",
        f"| Sin proyecto (la marca no replicaría) | {scan['no_project']} |",
        f"| Candidatas | {scan['candidates']} |",
        f"| Documentos en este PR | {len(result.entries)} |",
        f"| Choques con documentos existentes | {len(result.collisions)} |",
        "",
        "## Documentos",
        "",
    ]
    if result.entries:
        lines += ["| Documento | Tipo | Observación |", "| --- | --- | --- |"]
        for entry in result.entries:
            lines.append(f"| `{entry.knowledge_ref}` | `{entry.obs_type}` | `{entry.sync_id}` |")
    else:
        lines.append("Ninguno.")
    lines.append("")

    if result.collisions:
        lines += [
            "## Choques sin resolver",
            "",
            "Estas observaciones NO están en el patch: ya hay un documento curado en",
            "esa ruta y sobrescribirlo es justo lo que este bridge no hace. Resuélvelas",
            "a mano — fusiona el contenido en el documento existente y marca la",
            "observación con `promote stamp` apuntando a él.",
            "",
            "| Observación | Ruta ocupada |",
            "| --- | --- |",
        ]
        for collision in result.collisions:
            lines.append(f"| `{collision['sync_id']}` | `{collision['vault_path']}` |")
        lines.append("")

    lines += [
        "## Cómo aplicarlo",
        "",
        "En la raíz del checkout del repositorio del vault:",
        "",
        "```bash",
        f"git switch -c {manifest['branch']}",
        f"git apply --check {result.run_id}/promotion.patch   # falla antes de tocar nada",
        f"git apply {result.run_id}/promotion.patch",
        "```",
        "",
        "## Revisión",
        "",
        f"- Cada documento entra con `status: {config.PROMOTION_DOC_STATUS}`. Súbelo cuando lo hayas leído.",
        f"- `owner` entra como `{config.PROMOTION_DEFAULT_OWNER}`. Cámbialo al rol que corresponda "
        "(la tabla está en `Conventions/Frontmatter.md`).",
        "- El cuerpo viene de la observación tal cual. Corrige redacción y estructura aquí,",
        "  no en memory.",
        "",
        "## Después de fusionar",
        "",
        "```bash",
        "uv run python scripts/promote-memories.py stamp \\",
        f"  --manifest {result.run_id}/manifest.json",
        "```",
        "",
        "Ese paso marca cada observación con su `knowledge_ref`. Antes de la fusión no",
        "debe correrse: la referencia apuntaría a un documento que ningún checkout tiene,",
        "que es lo que `memory doctor --check knowledge_ref_dangling` reporta como defecto.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# stamp
# ---------------------------------------------------------------------------


@dataclass
class StampOutcome:
    stamped: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    missing: list[dict[str, str]] = field(default_factory=list)


def run_stamp(*, memory_bin: Path, manifest_path: Path, dry_run: bool) -> StampOutcome:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BridgeError(f"no se pudo leer el manifiesto {manifest_path}: {exc}", EXIT_NOTHING_TO_INSPECT) from exc
    except ValueError as exc:
        raise BridgeError(f"el manifiesto {manifest_path} no es JSON válido: {exc}", EXIT_NOTHING_TO_INSPECT) from exc

    entries = manifest.get("entries") or []
    if not entries:
        raise BridgeError(
            f"el manifiesto {manifest_path} no tiene entradas: no hay nada que marcar.",
            EXIT_NOTHING_TO_INSPECT,
        )

    project = str(manifest.get("project", "")).strip()
    if not project:
        raise BridgeError(f"el manifiesto {manifest_path} no declara `project`", EXIT_NOTHING_TO_INSPECT)

    vault = Path(manifest.get("vault_path") or config.VAULT_PATH)
    if not vault.is_dir():
        raise BridgeError(
            f"el vault del manifiesto no existe o no es un directorio: {vault}",
            EXIT_NOTHING_TO_INSPECT,
        )

    outcome = StampOutcome()
    for entry in entries:
        ref = str(entry["knowledge_ref"])
        sync_id = str(entry["sync_id"])
        # The existence check is the whole point of this half: memory validates
        # the *shape* of a knowledge_ref but never reads the vault, so nothing
        # else stops a reference to a document that was never merged.
        if not (vault / ref).is_file():
            outcome.missing.append({"sync_id": sync_id, "knowledge_ref": ref})
            continue
        if dry_run:
            outcome.stamped.append(sync_id)
            continue
        envelope = run_memory(
            memory_bin,
            ["project", project, "promote", "stamp", sync_id, "--knowledge-ref", ref, "--json"],
        )
        result = envelope.get("result") or {}
        if result.get("stamped"):
            outcome.stamped.append(sync_id)
        else:
            outcome.already.append(sync_id)

    return outcome


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--memory-bin", type=Path, default=_DEFAULT_MEMORY_BIN, help="ruta del binario de memory")
    parser.add_argument("--dry-run", action="store_true", help="reporta sin escribir nada")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="genera el paquete de promoción (no toca el vault ni memory)")
    plan.add_argument("--project", required=True, help="proyecto de memory a escanear")
    plan.add_argument("--types", default=None, help="subconjunto de la allowlist, separado por comas")
    plan.add_argument("--limit", type=int, default=0, help="tope de candidatos (0 = sin tope)")
    plan.add_argument("--output-dir", type=Path, default=None, help="raíz de salida (por omisión, config.json)")
    plan.add_argument("--run-id", default="", help="identificador de la corrida (por omisión, UTC)")
    _add_common(plan)

    stamp = sub.add_parser("stamp", help="marca knowledge_ref en memory tras fusionar el PR")
    stamp.add_argument("--manifest", type=Path, required=True, help="manifest.json producido por `plan`")
    _add_common(stamp)

    return parser


def _cmd_plan(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    run_id = args.run_id.strip() or now.strftime("%Y%m%dT%H%M%SZ")
    output_root = args.output_dir or config.PROMOTION_OUTPUT_DIR
    types = (
        [t.strip() for t in args.types.split(",") if t.strip()]
        if args.types is not None
        else []
    )

    # Reject empty --types if explicitly provided
    if args.types is not None and not types:
        print(
            f"ERROR: --types '{args.types}' es vacío. Debe ser una lista no-vacía de tipos "
            "(ej: 'decision,discovery' o solo 'decision'). Omítelo para usar la allowlist completa.",
            file=sys.stderr,
        )
        return EXIT_NOTHING_TO_INSPECT

    result = build_plan(
        memory_bin=args.memory_bin,
        project=args.project,
        types=types,
        limit=args.limit,
        output_root=Path(output_root),
        run_id=run_id,
        today=now.date().isoformat(),
        dry_run=args.dry_run,
    )

    scan = result.scan
    print(
        f"inspeccionadas {scan.get('pinned_inspected', 0)} observación(es) pinneada(s) · "
        f"candidatas {len(scan.get('candidates') or [])} · "
        f"ya promovidas {scan.get('already_promoted', 0)} · "
        f"fuera de la allowlist {scan.get('type_excluded', 0)}"
    )
    if scan.get("not_syncable") or scan.get("no_project"):
        print(
            f"no promovibles tal cual: {scan.get('not_syncable', 0)} sin sync_id, "
            f"{scan.get('no_project', 0)} sin proyecto"
        )
    print(f"allowlist aplicada: {', '.join(scan.get('types', []))}")

    if args.dry_run:
        print(f"[dry-run] se habrían escrito {len(result.entries)} documento(s); no se escribió nada")
    else:
        print(f"paquete en {result.output_dir}")
        print(f"  documentos: {len(result.entries)} · patch: promotion.patch · manifiesto: manifest.json")

    for entry in result.entries:
        print(f"  + {entry.knowledge_ref}  <- {entry.sync_id} ({entry.obs_type})")

    if result.collisions:
        print(f"CHOQUES: {len(result.collisions)} candidata(s) apuntan a un documento que ya existe:")
        for collision in result.collisions:
            print(f"  ! {collision['vault_path']}  <- {collision['sync_id']}")
        print("Esas quedan fuera del patch. Resuélvelas a mano antes de abrir el PR.")
        return EXIT_PARTIAL

    if not result.entries:
        print("no hay candidatas nuevas: todo lo pinneado y elegible ya está promovido.")
    return EXIT_OK


def _cmd_stamp(args: argparse.Namespace) -> int:
    outcome = run_stamp(memory_bin=args.memory_bin, manifest_path=args.manifest, dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"{prefix}marcadas {len(outcome.stamped)} · ya marcadas {len(outcome.already)} · "
        f"pendientes {len(outcome.missing)}"
    )
    for sync_id in outcome.stamped:
        print(f"  ✓ {sync_id}")
    for sync_id in outcome.already:
        print(f"  = {sync_id} (ya tenía la marca)")
    if outcome.missing:
        print("PENDIENTES: estos documentos todavía no están en el vault, así que no se marcó nada por ellos:")
        for miss in outcome.missing:
            print(f"  ! {miss['knowledge_ref']}  <- {miss['sync_id']}")
        print("Fusiona el pull request y vuelve a correr este comando.")
        return EXIT_PARTIAL
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            return _cmd_plan(args)
        return _cmd_stamp(args)
    except BridgeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code
    except (PermissionError, FileNotFoundError, OSError) as exc:
        print(
            f"ERROR: no se puede escribir en el directorio de salida ({exc}). "
            "Verifica los permisos y el espacio disponible.",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT


if __name__ == "__main__":
    raise SystemExit(main())
