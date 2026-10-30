"""Mirror the Jira issues of a vault's projects into tasks of its memory project.

The ``jira`` extension reads the issues of the projects a vault declares under
``integrations.jira`` (``jira_client``) and upserts one memory task per issue,
keyed by ``jira_key``, through the memory-bridge writer (``memory_bridge``).
It is served over stdio only: the memory server listens on the person's own
loopback.

A run, in order:

1. Checks before any request: the vault declares ``jira`` (``E_FEATURE_DISABLED``)
   and a ``memory.project`` (``E_MEMORY_PROJECT_MISSING``); ``integrations.jira``
   names a ``site``, ``projects`` and both secret variables
   (``E_MANIFEST_INVALID``), which hold a value (``E_SECRET_MISSING``, names
   only); for a write, the memory writer may write (a bearer unless
   ``KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0``, a loopback URL).
2. ``since``: the argument (an ISO date or date-time, or ``-Nd``) as given;
   else the stored cursor, minus a day of overlap after a complete run
   (the upsert is idempotent) and exactly after a truncated one; else thirty
   days ago. JQL reads dates in the time zone of the account, so ``since`` is
   written in the zone ``GET /rest/api/3/myself`` gives (UTC-12 when it
   gives none, which never starts later than the cursor).
3. JQL ``project in (<projects>) AND updated >= "<since>" [AND (<jql_extra>)]
   ORDER BY updated ASC, key ASC``, paginated up to :data:`MAX_ISSUES`. A
   truncated search whose issues all share the minute it starts from cannot
   move the cursor and is ``E_JIRA_SYNC_STALLED``. Then every key memory
   refused on the last run is read again by key.
4. One task body per issue: ``jira_key``, ``title``, ``kind`` (``kind_map``
   over a default per issue type), ``jira_status`` and
   ``jira_status_category`` as Jira gives them, ``assignee`` when there is
   one, ``state`` only when ``status_map`` names the status. memory derives
   the state from the Jira status itself otherwise.
5. Sequential upserts (memory is a local SQLite database). A task memory
   refuses on its merits is ``skipped`` with memory's own code and its key
   kept for the next run; a memory that stops answering ends the run and
   every task left is ``skipped``.
6. ``jira_sync.json`` in the vault's index directory keeps the cursor
   (``next_since``: the ``updated`` of the last issue read), whether the run
   was truncated and the refused keys, only after a write run where Jira and
   memory both answered; a dry run reads Jira and writes nothing anywhere.

Neither the credentials nor the bodies Jira or memory send back reach a
report, a message or a log line.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from knowledge import errors, extensions
from knowledge.errors import KnowledgeToolError, MemoryWriteFailed
from knowledge.jira_client import MAX_ISSUES, JiraClient
from knowledge.memory_bridge import MemoryWriter, writer_for
from knowledge.settings import (
    JIRA_PROJECT_PATTERN,
    MemorySettings,
    Settings,
    VaultSpec,
    integration_sites,
    jira_content_problems,
)

log = logging.getLogger(__name__)

#: File in the vault's index directory that keeps the cursor and the last attempt.
STATE_FILE = "jira_sync.json"
#: How far back a first run reads.
DEFAULT_SINCE = timedelta(days=30)
#: How far before the stored cursor every later run starts again.
OVERLAP = timedelta(hours=24)
#: Zone JQL dates are written in when Jira does not give the account's own:
#: the westernmost offset, so a written minute never stands for a moment
#: later than the cursor, whatever zone Jira reads it in.
FALLBACK_ZONE: tzinfo = timezone(timedelta(hours=-12))
#: Keys of refused issues a run reads again by key on the next one.
MAX_RETRY = 50
#: Minutes between two runs the session hook starts for one vault.
DEFAULT_THROTTLE_MINUTES = 60
#: Fields asked of Jira for every issue.
FIELDS: tuple[str, ...] = ("summary", "status", "issuetype", "assignee", "updated")
#: Longest title sent to memory.
TITLE_MAX = 300
#: A Jira issue key, as memory validates ``jira_key``.
JIRA_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
#: Status categories memory accepts as ``jira_status_category``.
STATUS_CATEGORIES: frozenset[str] = frozenset({"new", "indeterminate", "done"})
#: ``kind`` per Jira issue type when the vault's ``kind_map`` does not name it.
DEFAULT_KINDS: Mapping[str, str] = {"Bug": "bugfix", "Incident": "incident", "Spike": "spike"}
#: ``kind`` of an issue type no map names.
DEFAULT_KIND = "feature"
#: ``-Nd``: N days before now.
_RELATIVE = re.compile(r"^-(\d{1,4})d$")

Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# What the vault declares
# ---------------------------------------------------------------------------


def jira_block(spec: VaultSpec) -> Mapping[str, Any]:
    """``integrations.jira`` of ``spec``; empty when the vault declares none."""
    block = spec.integrations.get("jira")
    return block if isinstance(block, Mapping) else {}


def _mapping(block: Mapping[str, Any], key: str) -> Mapping[str, str]:
    value = block.get(key)
    if not isinstance(value, Mapping):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def throttle_minutes(spec: VaultSpec) -> int:
    """``integrations.jira.sync.throttle_minutes`` of ``spec``, or :data:`DEFAULT_THROTTLE_MINUTES`."""
    sync = jira_block(spec).get("sync")
    minutes = sync.get("throttle_minutes") if isinstance(sync, Mapping) else None
    if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes >= 0:
        return minutes
    return DEFAULT_THROTTLE_MINUTES


def _projects(block: Mapping[str, Any]) -> list[str]:
    projects = block.get("projects")
    if not isinstance(projects, Sequence) or isinstance(projects, str):
        return []
    return [key for key in projects if isinstance(key, str) and JIRA_PROJECT_PATTERN.fullmatch(key)]


def _require_feature(spec: VaultSpec) -> str:
    if "jira" not in spec.features:
        raise KnowledgeToolError(
            errors.E_FEATURE_DISABLED,
            f"{spec.slug}: the vault does not declare the jira feature; add it to features in vault.yaml",
        )
    if not spec.memory_project:
        raise KnowledgeToolError(
            errors.E_MEMORY_PROJECT_MISSING, f"{spec.slug}: the vault declares no memory.project to mirror into"
        )
    # The settings refuse these keys at load over stdio and only warn over
    # HTTP; a run checks them again whatever transport loaded the settings.
    problems = jira_content_problems(jira_block(spec))
    if problems:
        where, why = problems[0]
        raise KnowledgeToolError(errors.E_MANIFEST_INVALID, f"{spec.slug}: {where} {why}")
    return spec.memory_project


def _credential_names(spec: VaultSpec) -> tuple[str, str]:
    """The variables the e-mail and the API token come from; ``E_MANIFEST_INVALID`` when one is not named."""
    block = jira_block(spec)
    secrets = block.get("secrets")
    names: list[str] = []
    for key in ("email_env", "token_env"):
        name = secrets.get(key) if isinstance(secrets, Mapping) else None
        if not (isinstance(name, str) and name.strip()):
            raise KnowledgeToolError(
                errors.E_MANIFEST_INVALID,
                f"{spec.slug}: integrations.jira.secrets.{key} must name the variable that carries the credential",
            )
        names.append(name.strip())
    return names[0], names[1]


def client_for(spec: VaultSpec, env: Mapping[str, str]) -> JiraClient:
    """The Jira client of ``spec``, with the credentials ``env`` holds under the names the vault declares."""
    block = jira_block(spec)
    site = block.get("site")
    if not (isinstance(site, str) and site.strip()):
        raise KnowledgeToolError(errors.E_MANIFEST_INVALID, f"{spec.slug}: integrations.jira.site is required")
    if not _projects(block):
        raise KnowledgeToolError(
            errors.E_MANIFEST_INVALID, f"{spec.slug}: integrations.jira.projects must list at least one project key"
        )
    email_env, token_env = _credential_names(spec)
    missing = [name for name in (email_env, token_env) if not (env.get(name) or "").strip()]
    if missing:
        raise KnowledgeToolError(
            errors.E_SECRET_MISSING,
            f"{spec.slug}: the jira extension needs {', '.join(missing)}; export them in the environment "
            "of this process",
        )
    return JiraClient(site, env[email_env].strip(), env[token_env].strip(), sites=integration_sites(env))


# ---------------------------------------------------------------------------
# since, JQL and the task body
# ---------------------------------------------------------------------------


def parse_since(raw: str, now: datetime) -> datetime:
    """``raw`` as an aware UTC moment: ``-Nd``, an ISO date (midnight UTC) or an ISO date-time (UTC when naive)."""
    text = raw.strip()
    relative = _RELATIVE.fullmatch(text)
    if relative:
        return now - timedelta(days=int(relative.group(1)))
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise KnowledgeToolError(
            errors.E_ARGUMENT_OUT_OF_RANGE,
            "since must be an ISO date (2026-09-01), an ISO date-time (2026-09-01T12:00:00Z) or -Nd (-7d)",
        ) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    if moment > now:
        raise KnowledgeToolError(errors.E_ARGUMENT_OUT_OF_RANGE, "since is in the future")
    return moment.astimezone(UTC)


def build_jql(projects: Sequence[str], since: datetime, jql_extra: str = "", zone: tzinfo = UTC) -> str:
    """The search of one run: issues of ``projects`` updated at or after ``since``, oldest first, then by key.

    JQL reads a date in the time zone of the account that runs the search,
    so ``since`` is written in ``zone``, that account's zone.
    """
    jql = f'project in ({", ".join(projects)}) AND updated >= "{since.astimezone(zone):%Y-%m-%d %H:%M}"'
    extra = jql_extra.strip()
    if extra:
        jql += f" AND ({extra})"
    return jql + " ORDER BY updated ASC, key ASC"


def issue_updated(issue: Mapping[str, Any]) -> datetime | None:
    """``fields.updated`` of ``issue`` as an aware UTC moment, or ``None`` when it does not parse."""
    fields = issue.get("fields")
    raw = fields.get("updated") if isinstance(fields, Mapping) else None
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)).astimezone(UTC)


def _name(node: Any, key: str = "name") -> str:
    value = node.get(key) if isinstance(node, Mapping) else None
    return value.strip() if isinstance(value, str) else ""


def task_body(
    issue: Mapping[str, Any], kind_map: Mapping[str, str], status_map: Mapping[str, str]
) -> tuple[dict[str, str] | None, str | None]:
    """The memory task of ``issue`` and, when its status category is not one memory knows, its status name.

    ``None`` for the body when the key is not a Jira issue key.
    """
    key = issue.get("key")
    if not (isinstance(key, str) and JIRA_KEY_PATTERN.fullmatch(key)):
        return None, None
    fields = issue.get("fields")
    fields = fields if isinstance(fields, Mapping) else {}
    issue_type = _name(fields.get("issuetype"))
    kinds = {**DEFAULT_KINDS, **kind_map}
    summary = fields.get("summary")
    body: dict[str, str] = {
        "jira_key": key,
        "title": (summary.strip() if isinstance(summary, str) and summary.strip() else key)[:TITLE_MAX],
        "kind": kinds.get(issue_type) or kinds.get("default") or DEFAULT_KIND,
    }
    status = fields.get("status")
    status_name = _name(status)
    unmapped: str | None = None
    if status_name:
        body["jira_status"] = status_name
        category = _name(status.get("statusCategory") if isinstance(status, Mapping) else None, "key")
        if category in STATUS_CATEGORIES:
            body["jira_status_category"] = category
        elif status_name not in status_map:
            unmapped = status_name
        if status_name in status_map:
            body["state"] = status_map[status_name]
    assignee = _name(fields.get("assignee"), "displayName")
    if assignee:
        body["assignee"] = assignee
    return body, unmapped


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def state_path(spec: VaultSpec) -> Path:
    return spec.index_dir / STATE_FILE


def read_state(spec: VaultSpec) -> dict[str, Any]:
    """The content of ``jira_sync.json``; empty when it is missing or unreadable."""
    try:
        payload = json.loads(state_path(spec).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(spec: VaultSpec, state: Mapping[str, Any]) -> None:
    """Replace ``jira_sync.json`` atomically, through a temporary file no other run shares."""
    path = state_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{STATE_FILE}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _moment(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)).astimezone(UTC)


def _retry_keys(state: Mapping[str, Any]) -> list[str]:
    keys = state.get("retry")
    if not isinstance(keys, list):
        return []
    return sorted({key for key in keys if isinstance(key, str) and JIRA_KEY_PATTERN.fullmatch(key)})[:MAX_RETRY]


def due(spec: VaultSpec, now: datetime) -> bool:
    """Whether the throttle of ``spec`` lets a session start another run at ``now``."""
    last = _moment(read_state(spec).get("last_attempt_at"))
    return last is None or now - last >= timedelta(minutes=throttle_minutes(spec))


def mark_attempt(spec: VaultSpec, now: datetime) -> None:
    """Record in ``jira_sync.json`` that a run starts at ``now``, keeping the cursor."""
    _write_state(spec, {**read_state(spec), "last_attempt_at": _iso(now)})


def _resolve_since(state: Mapping[str, Any], since: str | None, now: datetime) -> datetime:
    """Where a run starts: ``since``, else the stored cursor, else :data:`DEFAULT_SINCE` ago.

    The stored cursor goes back :data:`OVERLAP` after a complete run; after a
    truncated one the run resumes exactly where it stopped, so a backlog
    larger than one run always moves forward.
    """
    if since is not None and since.strip():
        return parse_since(since, now)
    cursor = _moment(state.get("cursor"))
    if cursor is None:
        return now - DEFAULT_SINCE
    cursor = min(cursor, now)
    return cursor if state.get("truncated") is True else cursor - OVERLAP


def jql_zone(client: JiraClient) -> tzinfo:
    """The zone JQL reads dates in for ``client``'s account, or :data:`FALLBACK_ZONE` when Jira does not say."""
    name = client.time_zone()
    if not name:
        return FALLBACK_ZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return FALLBACK_ZONE


def _minute(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(second=0, microsecond=0)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def sync(
    spec: VaultSpec,
    *,
    env: Mapping[str, str],
    memory: MemorySettings,
    dry_run: bool = True,
    since: str | None = None,
    now: Clock = _now,
    client: JiraClient | None = None,
    writer: MemoryWriter | None = None,
    max_issues: int = MAX_ISSUES,
) -> dict[str, Any]:
    """Mirror the Jira issues of ``spec`` into memory (or, with ``dry_run``, only read them); return the report.

    The report: ``vault``, ``memory_project``, ``dry_run``, ``jql``, ``since``,
    ``next_since``, ``truncated``, ``fetched``, ``retried``, ``upserted``,
    ``created``, ``skipped`` (``[{key, reason}]``), ``status_unmapped``, and
    ``error`` (``{code, message}``) when memory stopped answering mid-run. A
    failure before the first write raises
    :class:`~knowledge.errors.KnowledgeToolError`.

    The cursor never passes an issue that was not mirrored: a task memory
    refuses is read again by key on the next run (up to :data:`MAX_RETRY`
    keys; past them the cursor stays at the oldest refused issue), an
    explicit ``since`` later than the stored cursor leaves the cursor where it
    was, and a truncated run resumes from its last issue without the overlap.
    """
    if not 1 <= max_issues <= MAX_ISSUES:
        raise KnowledgeToolError(errors.E_ARGUMENT_OUT_OF_RANGE, f"max_issues must be between 1 and {MAX_ISSUES}")
    project = _require_feature(spec)
    if client is None:
        client = client_for(spec, env)
    if not dry_run and writer is None:
        try:
            writer = writer_for(memory)
        except MemoryWriteFailed as exc:
            raise KnowledgeToolError(exc.code, exc.message) from None
    started = now()
    previous = read_state(spec)
    if not dry_run:
        mark_attempt(spec, started)
    block = jira_block(spec)
    since_at = _resolve_since(previous, since, started)
    jql_extra = block.get("jql_extra")
    zone = jql_zone(client)
    jql = build_jql(_projects(block), since_at, jql_extra if isinstance(jql_extra, str) else "", zone)
    result = client.search(jql, FIELDS, max_issues=max_issues)
    read_max = max((moment for moment in map(issue_updated, result.issues) if moment is not None), default=None)
    if result.truncated and len(result.issues) >= max_issues and (
        read_max is None or _minute(read_max) <= _minute(since_at)
    ):
        raise KnowledgeToolError(
            errors.E_JIRA_SYNC_STALLED,
            f"{spec.slug}: the {len(result.issues)} issues of this run were all updated in the minute "
            f"{_minute(since_at):%Y-%m-%d %H:%M} UTC, and JQL compares minutes, so the cursor cannot move past "
            "it; raise --max-issues, narrow integrations.jira.jql_extra, or pass a later since",
        )

    seen = {issue.get("key") for issue in result.issues}
    retried = [
        found
        for key in _retry_keys(previous)
        if key not in seen and (found := client.issue(key, FIELDS)) is not None
    ]
    issues = [*retried, *result.issues]

    kind_map, status_map = _mapping(block, "kind_map"), _mapping(block, "status_map")
    report: dict[str, Any] = {
        "vault": spec.slug,
        "memory_project": project,
        "dry_run": dry_run,
        "jql": jql,
        "since": _iso(since_at),
        "next_since": _iso(since_at),
        "truncated": result.truncated,
        "fetched": len(result.issues),
        "retried": len(retried),
        "upserted": 0,
        "created": 0,
        "skipped": [],
        "status_unmapped": [],
    }
    unmapped: set[str] = set()
    tasks: list[tuple[dict[str, str], datetime | None]] = []
    for issue in issues:
        body, status = task_body(issue, kind_map, status_map)
        if status is not None:
            unmapped.add(status)
        if body is None:
            key = issue.get("key")
            report["skipped"].append({"key": key if isinstance(key, str) else "", "reason": "invalid_key"})
            continue
        tasks.append((body, issue_updated(issue)))
    report["status_unmapped"] = sorted(unmapped)
    stored = _moment(previous.get("cursor"))
    explicit_later = since is not None and since.strip() != "" and stored is not None and since_at > stored
    if dry_run or writer is None:
        report["next_since"] = _iso(_next_cursor(since_at, read_max, stored, explicit_later, None))
        return report

    refused, failure = _upsert_all(writer, project, tasks, report)
    if failure is not None:
        report["error"] = {"code": failure.code, "message": failure.message}
        return report
    kept, overflow = refused[:MAX_RETRY], refused[MAX_RETRY:]
    pin = min((moment for _key, moment in overflow if moment is not None), default=None)
    report["next_since"] = _iso(_next_cursor(since_at, read_max, stored, explicit_later, pin))
    truncated = previous.get("truncated") is True if explicit_later else result.truncated
    _write_state(
        spec,
        {
            **read_state(spec),
            "cursor": report["next_since"],
            "truncated": truncated,
            "retry": sorted(key for key, _ in kept),
            "synced_at": _iso(started),
            "last_result": {key: report[key] for key in ("fetched", "retried", "upserted", "created")}
            | {"skipped": len(report["skipped"])},
        },
    )
    return report


def _next_cursor(
    since_at: datetime,
    read_max: datetime | None,
    stored: datetime | None,
    explicit_later: bool,
    pin: datetime | None,
) -> datetime:
    """Where the next run resumes.

    The ``updated`` of the last issue read (or ``since_at`` when none was),
    never before the stored cursor (an empty run does not walk it back a day
    at a time), never after it when an explicit ``since`` skipped the issues
    between them, and never after ``pin``, the oldest refused issue the retry
    list had no room for.
    """
    cursor = max(read_max, since_at) if read_max is not None else since_at
    if stored is not None:
        cursor = stored if explicit_later else max(cursor, stored)
    if pin is not None:
        cursor = min(cursor, pin)
    return cursor


def _upsert_all(
    writer: MemoryWriter,
    project: str,
    tasks: Sequence[tuple[dict[str, str], datetime | None]],
    report: dict[str, Any],
) -> tuple[list[tuple[str, datetime | None]], MemoryWriteFailed | None]:
    """Upsert ``tasks`` one by one into ``report``.

    Returns the tasks memory refused on their merits, oldest first, as
    ``(key, updated)``, and the failure that stopped the run, if any.
    """
    refused: list[tuple[str, datetime | None]] = []
    for index, (body, updated) in enumerate(tasks):
        try:
            outcome = writer.upsert_task(project, body)
        except MemoryWriteFailed as exc:
            reason = "memory_unavailable" if exc.code == errors.E_MEMORY_UNAVAILABLE else (exc.reason or exc.code)
            report["skipped"].extend({"key": rest["jira_key"], "reason": reason} for rest, _ in tasks[index:])
            log.warning("jira sync stopped: %s after %d of %d tasks", exc.code, index, len(tasks))
            return refused, exc
        if outcome["outcome"] == "skipped":
            report["skipped"].append({"key": body["jira_key"], "reason": outcome.get("reason") or "skipped"})
            refused.append((body["jira_key"], updated))
            continue
        report["upserted"] += 1
        if outcome["outcome"] == "created":
            report["created"] += 1
    oldest = datetime.max.replace(tzinfo=UTC)
    return sorted(refused, key=lambda row: (row[1] or oldest, row[0])), None


def auto(settings: Settings, env: Mapping[str, str], *, now: Clock = _now) -> list[dict[str, Any]]:
    """The run the session hook starts: every vault that declares ``jira`` and is due, quietly.

    A vault whose credentials or memory bearer are not exported is reported
    as ``skipped`` without a request; one that ran within its
    ``sync.throttle_minutes`` too. Nothing raises: a vault whose run fails
    for any reason is reported as ``failed`` with the error code, or the
    class of an unexpected error, and the next vault still runs.
    """
    reports: list[dict[str, Any]] = []
    ext = extensions.get("jira")
    for spec in settings.vaults:
        if "jira" not in spec.features:
            continue
        row: dict[str, Any] = {"vault": spec.slug}
        try:
            missing = extensions.missing_env(spec, ext, env)
            if not due(spec, now()):
                reports.append({**row, "status": "skipped", "reason": "throttled"})
                continue
            if missing:
                reports.append({**row, "status": "skipped", "reason": "unconfigured"})
                continue
            report = sync(spec, env=env, memory=settings.memory, dry_run=False, now=now)
        except KnowledgeToolError as exc:
            reports.append({**row, "status": "failed", "code": exc.code})
            continue
        except Exception as exc:  # noqa: BLE001 - the session hook's run never raises
            log.warning("jira sync of %s failed: %s", spec.slug, type(exc).__name__)
            reports.append({**row, "status": "failed", "reason": type(exc).__name__})
            continue
        status = "failed" if "error" in report else "synced"
        reports.append({**row, "status": status, **{k: report[k] for k in ("fetched", "upserted", "created")}})
    return reports
