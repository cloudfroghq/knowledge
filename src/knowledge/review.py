"""Review-decay: which vault docs need revalidation, and who owns them.

Two concerns live together here because the ``get_stale_docs`` MCP tool and
the offline weekly report must never disagree. The tool reads the documents
of the vault's index; the report (``scripts/weekly-stale-report.py``) reads vault
frontmatter straight off disk so it can run in CI with no index, no API key
and no vector store. Both call the functions below, so a threshold, an
exclusion rule or an ownership rule is defined exactly once.

Ownership follows the vault contract documented in
``Conventions/Frontmatter.md``: ``owner`` is mandatory on ``type: service``
hubs and absent everywhere else. Runbooks, playbooks and flows declare
``service`` instead, so their owner is resolved through the chain
``doc.service -> service hub -> hub.owner``. Resolving it in code is what
lets a per-owner report exist without duplicating the field across hundreds
of human-curated documents.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Any

from knowledge import config

# Frontmatter ``type`` of the documents that declare ownership.
SERVICE_HUB_TYPE = "service"

# Owner reported when the chain resolves to nothing. A real role name would
# silently hand the work to a team that never accepted it.
UNASSIGNED = "unassigned"

# How a document's owner was obtained, carried alongside the owner so a
# reader can tell a declared owner from an inferred one.
OWNER_FROM_FRONTMATTER = "frontmatter"
OWNER_FROM_SERVICE_HUB = "service-hub"
OWNER_UNRESOLVED = "unresolved"

# The three things a ``last_updated`` field can be. The middle one is the
# reason this enum exists at all: a date nobody can parse is not "no data",
# it is data that could not be read, and collapsing the two makes the
# document invisible to every review route instead of noisy in one.
LAST_UPDATED_MISSING = "missing"
LAST_UPDATED_INVALID = "invalid"
LAST_UPDATED_DATED = "dated"

# Why a document is on a review queue. Carried on every row so a consumer
# can tell "overdue by N days" from "I could not tell how old this is".
REASON_OVERDUE = "overdue"
REASON_LAST_UPDATED_INVALID = "last_updated_invalid"
# Only on request: a document with no ``last_updated`` at all has no decay to
# measure, so the default queue leaves it out.
REASON_LAST_UPDATED_MISSING = "last_updated_missing"

_DEFAULT_THRESHOLD_KEY = "default"
_FALLBACK_THRESHOLD_DAYS = 270


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def is_curated(
    rel_path: str,
    *,
    memory_folder: str | None = None,
    index_exclude: Callable[[str], bool] | None = None,
) -> bool:
    """True when a vault-relative path holds human-curated knowledge.

    Excluded, in both the tool and the report:

    - anything under a dot-directory — Obsidian's ``.trash`` (already
      deleted), and plugin state such as ``.space`` or ``.obsidian``;
    - the memory consolidation folder (``config.MEMORY_FOLDER``), which
      holds machine-exported memories already fenced off from search;
    - what the vault's own ``index.exclude`` covers (``index_exclude`` is the
      predicate built from those globs): those files are not notes, and the
      vault linter does not hold them to the frontmatter rules either.

    Flagging any of those "needs review" sends an owner to revalidate a file
    nobody curates. ``memory_folder`` defaults to the configured one.
    """
    if not rel_path:
        return False
    parts = PurePosixPath(str(rel_path).replace("\\", "/")).parts
    if not parts:
        return False
    if any(part.startswith(".") for part in parts):
        return False
    if parts[0] == (config.MEMORY_FOLDER if memory_folder is None else memory_folder):
        return False
    return not (index_exclude is not None and index_exclude("/".join(parts)))


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def threshold_for(doc_type: str | None, thresholds: Mapping[str, int] | None = None) -> int:
    """Days a document of ``doc_type`` may go unrevised.

    ``thresholds`` is the vault's ``review.thresholds_days``; it defaults to
    the configured one (config.json).
    """
    if thresholds is None:
        thresholds = config.REVIEW_THRESHOLDS_DAYS
    return int(
        thresholds.get(
            str(doc_type or ""),
            thresholds.get(_DEFAULT_THRESHOLD_KEY, _FALLBACK_THRESHOLD_DAYS),
        )
    )


def thresholds_are_configured() -> bool:
    """True when ``config.json`` actually declared ``review.thresholds_days``.

    ``threshold_for`` must answer for every document type, so it can never
    refuse; it always returns a number. That is exactly what makes the
    provenance invisible to a caller: a runbook resolves to 90 days when
    ``config.json`` is present and to the 270-day catch-all when it is not,
    and both look like a legitimate answer.

    This is the seam a gate uses to say "the numbers came from the repository"
    rather than "the numbers came from nowhere and happened to be plausible".
    """
    return bool(config.REVIEW_THRESHOLDS_FROM_CONFIG)


def last_updated_state(meta: Mapping[str, Any] | None) -> tuple[str, str]:
    """Classify a document's ``last_updated`` into (state, raw value).

    Three outcomes, never two:

    - ``LAST_UPDATED_MISSING``  — the field is absent or blank. Genuinely no
      data; nobody can be asked to revalidate on that basis.
    - ``LAST_UPDATED_INVALID``  — the field is there and no parser can read
      it (``not-a-date``, an unrendered ``YYYY-MM-DD`` placeholder). This is
      a broken document, and it has to be reported as such: treating it like
      the missing case hides it from the report *and* from the tool forever.
    - ``LAST_UPDATED_DATED``    — a real ISO date; ``review_status`` decides
      whether it is past its threshold.
    """
    raw = str((meta or {}).get("last_updated", "")).strip()
    if not raw:
        return LAST_UPDATED_MISSING, ""
    try:
        datetime.fromisoformat(raw)
    except ValueError:
        return LAST_UPDATED_INVALID, raw
    return LAST_UPDATED_DATED, raw


def review_status(
    meta: Mapping[str, Any] | None,
    *,
    today: date | None = None,
    thresholds: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Flag docs whose frontmatter last_updated exceeds their type's threshold.

    Review-decay marks knowledge for revalidation without penalising ranking:
    an old architecture doc may be perfectly valid, so this only annotates.
    A missing or unreadable date is never reported as "stale" — an age it
    cannot compute would be a fabricated number. Callers that need the
    unreadable case surfaced ask ``last_updated_state`` for it; this function
    answers one question only, and answers it truthfully.
    """
    state, raw = last_updated_state(meta)
    if state != LAST_UPDATED_DATED:
        return {}
    last = datetime.fromisoformat(raw).date()
    age_days = ((today or date.today()) - last).days
    if age_days > threshold_for((meta or {}).get("type"), thresholds):
        return {"needs_review": True, "age_days": age_days}
    return {}


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def build_service_owner_map(metas: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Map ``service`` -> ``owner`` from every ``type: service`` hub.

    Accepts any iterable of frontmatter-shaped mappings, so it works on the
    index's documents (``DocRecord.metadata()``) and on freshly parsed vault
    frontmatter alike. The first non-empty owner of a service wins, so a later
    empty one cannot erase it.
    """
    owners: dict[str, str] = {}
    for meta in metas:
        if not meta or str(meta.get("type", "")) != SERVICE_HUB_TYPE:
            continue
        service = _norm(meta.get("service"))
        owner = str(meta.get("owner", "")).strip()
        if service and owner and service not in owners:
            owners[service] = owner
    return owners


def resolve_owner(
    meta: Mapping[str, Any] | None, service_owners: Mapping[str, str]
) -> tuple[str, str]:
    """Return ``(owner, source)`` for a document.

    A declared ``owner`` in the document's own frontmatter always wins. When
    it is absent — the case for every runbook and playbook in the vault — the
    document's ``service`` is looked up against the service hubs.
    """
    if meta:
        declared = str(meta.get("owner", "")).strip()
        if declared:
            return declared, OWNER_FROM_FRONTMATTER
        service = _norm(meta.get("service"))
        if service:
            inherited = service_owners.get(service, "").strip()
            if inherited:
                return inherited, OWNER_FROM_SERVICE_HUB
    return UNASSIGNED, OWNER_UNRESOLVED


def known_owners(
    metas: Iterable[Mapping[str, Any]], service_owners: Mapping[str, str]
) -> set[str]:
    """Every owner the resolution chain can produce over a corpus.

    Used to reject a misspelled ``--owner`` before it turns an empty result
    into a green gate: a filter that matches nobody has not proven that
    nobody is overdue, it has proven the filter is wrong. ``UNASSIGNED`` is
    always part of the set — it is a documented bucket, not a typo.
    """
    owners = {UNASSIGNED}
    owners.update(str(o).strip() for o in service_owners.values() if str(o).strip())
    for meta in metas:
        owner, _ = resolve_owner(meta, service_owners)
        owners.add(owner)
    return owners


def group_by_owner(docs: Iterable[Mapping[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group stale documents by owner, oldest first inside each group.

    Owners are sorted alphabetically with ``UNASSIGNED`` pushed last: it is
    the bucket that needs a human decision (assign a hub owner, or declare
    one on the document), not a team to notify.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        groups.setdefault(str(doc.get("owner") or UNASSIGNED), []).append(dict(doc))

    def _owner_key(owner: str) -> tuple[int, str]:
        return (1, owner) if owner == UNASSIGNED else (0, owner)

    return [
        (
            owner,
            sorted(
                entries,
                key=lambda d: (-int(d.get("age_days") or 0), str(d.get("file_path") or "")),
            ),
        )
        for owner, entries in sorted(groups.items(), key=lambda kv: _owner_key(kv[0]))
    ]


def _norm(value: Any) -> str:
    """Normalise a frontmatter key used for joining (``service``)."""
    return str(value or "").strip().lower()
