"""Retrieval usage tracking: ranking boosts and co-retrieval signals.

Two signals are recorded from search/read events:

- Per-document usage counters, turned into a bounded ranking boost
  (max +10%) so frequently-consulted docs win ties without overriding
  semantic relevance. Counters decay with a 90-day half-life so old
  habits fade instead of fossilising the ranking.
- Co-retrieval pairs (docs appearing together in a result set), bucketed
  by ISO week. The link graph consumes the 30-day co-occurrence frequency
  to strengthen edges Hebbian-style (see graph.LinkGraph).
- Query -> open pairs: which document a client reads with ``get_document``
  after which search, and at what rank that search had returned it. This is
  the implicit relevance judgement the retrieval evaluation is calibrated
  against; it is recorded, never fed back into the ranking.
- Rerank aggregates (section ``rerank``): how many searches evaluated the
  judge, how many it reordered and why the rest were skipped, units judged
  and served from the cache, billed input tokens, cost, a latency histogram
  with fixed buckets and the number of searches per UTC day. Never a query or
  a passage. The spend of the current UTC day (``day``, ``usd_today``) is the
  one value shared between processes: it is re-read from disk before every
  judge call and written back as soon as a call costs something, so several
  stdio processes over the same vault share one daily cap. Every write of the
  file merges that spend with the one on disk under an exclusive lock on a
  ``.lock`` sidecar and replaces the file atomically, so a process whose copy
  is behind never lowers the spend another process recorded.
- Tool calls (section ``tool_calls``): how many MCP ``tools/call`` requests
  each tool received per UTC day, split by client (the ``clientInfo.name`` of
  the session's ``initialize``) and principal (the id of the HTTP bearer, or
  ``stdio``), each with how many of them ended in an error. Never an
  argument, a query or a path. A process keeps only the counts it has not
  written yet, and every write adds them onto the counts on disk under the
  same file lock as the spend, so several processes over one vault never
  overwrite each other's calls. Days older than 120 are dropped.

Attribution of an open to a query is by recency within one server process:
the open is credited to the newest recent search whose results contained the
document, or, when none did, to the newest recent search with ``rank`` left
empty (the client read something the search did not surface). A process that
serves several clients at once (the HTTP transport) can interleave their
searches, so its pairs are an approximation; a stdio process serves exactly
one client and its pairs are exact.

State lives in a JSON overlay in the vault's index directory. If it is deleted
or corrupted, everything regenerates from scratch — no data loss.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import threading
import time
from collections import deque
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from typing import Any

try:  # POSIX only; elsewhere the spend merge still runs, without the file lock.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_FLUSH_EVERY_HITS = 20
_HALF_LIFE_DAYS = 90.0
_PAIR_WINDOW_WEEKS = 5  # ~30 days of ISO-week buckets
_MIN_USES_KEPT = 0.05
# Searches an open can still be attributed to: the newest few, and only while
# they are recent enough to plausibly be the reason the document was opened.
_RECENT_QUERIES_KEPT = 20
_QUERY_ATTRIBUTION_SECONDS = 30 * 60
# Persisted query -> open events; the oldest are dropped past this many.
_MAX_QUERY_OPENS = 2000
#: Upper bounds, in milliseconds, of the rerank latency buckets; ``inf`` takes the rest.
RERANK_LATENCY_BUCKETS_MS: tuple[int, ...] = (50, 100, 200, 300, 500, 750, 1000, 1500, 3000)
_OPEN_BUCKET = "inf"
# UTC days of search counts kept in ``rerank.searches_by_day``.
_SEARCH_DAYS_KEPT = 31
#: UTC days of tool-call counts kept in ``tool_calls.days``.
TOOL_CALL_DAYS_KEPT = 120
#: Label of a tool call without a client name, or a principal, to report.
UNKNOWN_LABEL = "unknown"
#: Label the principal of a call takes when it arrives over stdio (no bearer at all).
STDIO_PRINCIPAL = "stdio"
#: Label that absorbs the distinct names past the per-day caps below.
OTHER_LABEL = "other"
# Longest kept label; longer names are cut. Tool names allow 64 characters
# in MCP; client names and principal ids are kept shorter.
_TOOL_LABEL_MAX = 64
_CLIENT_LABEL_MAX = 40
# Distinct labels one UTC day keeps at each level of ``tool_calls.days``;
# further names count under ``other``, so a client that sends random names
# cannot grow the file without bound.
_MAX_TOOLS_PER_DAY = 64
_MAX_CLIENTS_PER_TOOL = 32
_MAX_PRINCIPALS_PER_CLIENT = 16
_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _week_key(d: date | None = None) -> str:
    d = d or date.today()
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def _recent_week_keys(weeks: int = _PAIR_WINDOW_WEEKS) -> set[str]:
    today = date.today()
    return {_week_key(today - timedelta(weeks=i)) for i in range(weeks)}


def _pair_key(a: str, b: str) -> str:
    return "||".join(sorted((a, b)))


def utc_day() -> str:
    """Today's date in UTC, ``YYYY-MM-DD``: the day a rerank spending cap counts."""
    return datetime.now(timezone.utc).date().isoformat()


def _latency_bucket(latency_ms: float) -> str:
    for bound in RERANK_LATENCY_BUCKETS_MS:
        if latency_ms <= bound:
            return str(bound)
    return _OPEN_BUCKET


def _fresh_rerank() -> dict[str, Any]:
    return {
        "calls": 0,
        "applied": 0,
        "skipped": {},
        "units": 0,
        "cache_hits": 0,
        "input_tokens": 0,
        "usd": 0.0,
        "latency_ms": {},
        "day": "",
        "usd_today": 0.0,
        "searches_by_day": {},
    }


def _rerank_section(raw: Any) -> dict[str, Any]:
    """The ``rerank`` section of a usage payload, with every key present and well typed."""
    out = _fresh_rerank()
    if not isinstance(raw, dict):
        return out
    for key in ("calls", "applied", "units", "cache_hits", "input_tokens"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    for key in ("usd", "usd_today"):
        value = raw.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
            out[key] = float(value)
    if isinstance(raw.get("day"), str):
        out["day"] = raw["day"]
    for key in ("skipped", "latency_ms"):
        value = raw.get(key)
        if isinstance(value, dict):
            out[key] = {str(k): int(v) for k, v in value.items() if isinstance(v, int) and not isinstance(v, bool)}
    days = raw.get("searches_by_day")
    if isinstance(days, dict):
        out["searches_by_day"] = {
            str(day): {k: int(v) for k, v in counts.items() if isinstance(v, int) and not isinstance(v, bool)}
            for day, counts in days.items()
            if isinstance(counts, dict)
        }
    return out


def call_label(value: object, *, max_len: int = _CLIENT_LABEL_MAX) -> str:
    """``value`` as a tool-call label: ASCII letters, digits, ``.``, ``_`` and ``-`` only.

    Any other character becomes ``_`` and the result is cut to ``max_len``
    characters; a missing or blank value is :data:`UNKNOWN_LABEL`.
    """
    if not isinstance(value, str):
        return UNKNOWN_LABEL
    text = value.strip()
    if not text:
        return UNKNOWN_LABEL
    return "".join(ch if ch in _LABEL_CHARS else "_" for ch in text)[:max_len]


def _count_pair(raw: Any) -> tuple[int, int]:
    """``(calls, errors)`` of one ``tool_calls`` leaf; anything malformed reads as ``(0, 0)``."""
    if not isinstance(raw, dict):
        return 0, 0
    calls, errs = raw.get("calls"), raw.get("errors")
    calls = calls if isinstance(calls, int) and not isinstance(calls, bool) and calls >= 0 else 0
    errs = errs if isinstance(errs, int) and not isinstance(errs, bool) and errs >= 0 else 0
    return calls, min(errs, calls)


def _tool_call_days(raw: Any) -> dict[str, dict[str, dict[str, dict[str, dict[str, int]]]]]:
    """The ``days`` of a ``tool_calls`` section, well typed: ``day -> tool -> client -> principal -> leaf``."""
    out: dict[str, dict[str, dict[str, dict[str, dict[str, int]]]]] = {}
    days = raw.get("days") if isinstance(raw, dict) else None
    if not isinstance(days, dict):
        return out
    for day, tools in days.items():
        if not isinstance(day, str) or not isinstance(tools, dict):
            continue
        for tool, clients in tools.items():
            if not isinstance(clients, dict):
                continue
            for client, principals in clients.items():
                if not isinstance(principals, dict):
                    continue
                for principal, leaf in principals.items():
                    calls, errs = _count_pair(leaf)
                    if calls:
                        _add_call(out, day, str(tool), str(client), str(principal), calls, errs)
    return out


def _add_call(
    days: dict[str, dict[str, dict[str, dict[str, dict[str, int]]]]],
    day: str,
    tool: str,
    client: str,
    principal: str,
    calls: int,
    errs: int,
) -> None:
    """Add ``calls`` (``errs`` of them errors) under ``day``, folding names past the caps into ``other``."""
    tools = days.setdefault(day, {})
    if tool not in tools and len(tools) >= _MAX_TOOLS_PER_DAY:
        tool = OTHER_LABEL
    clients = tools.setdefault(tool, {})
    if client not in clients and len(clients) >= _MAX_CLIENTS_PER_TOOL:
        client = OTHER_LABEL
    principals = clients.setdefault(client, {})
    if principal not in principals and len(principals) >= _MAX_PRINCIPALS_PER_CLIENT:
        principal = OTHER_LABEL
    leaf = principals.setdefault(principal, {"calls": 0, "errors": 0})
    leaf["calls"] += calls
    leaf["errors"] += errs


def _merge_call_days(
    into: dict[str, dict[str, dict[str, dict[str, dict[str, int]]]]],
    extra: dict[str, dict[str, dict[str, dict[str, dict[str, int]]]]],
) -> None:
    for day, tools in extra.items():
        for tool, clients in tools.items():
            for client, principals in clients.items():
                for principal, leaf in principals.items():
                    _add_call(into, day, tool, client, principal, leaf["calls"], leaf["errors"])


def _kept_call_days(
    days: dict[str, dict[str, dict[str, dict[str, dict[str, int]]]]], today: str
) -> dict[str, dict[str, dict[str, dict[str, dict[str, int]]]]]:
    """``days`` without the ones older than :data:`TOOL_CALL_DAYS_KEPT` before ``today``, in day order."""
    cutoff = (date.fromisoformat(today) - timedelta(days=TOOL_CALL_DAYS_KEPT - 1)).isoformat()
    return {day: days[day] for day in sorted(days) if day >= cutoff}


def tool_calls_summary(
    path: Path,
    *,
    day: str | None = None,
    windows: tuple[int, ...] = (7, 30),
    registered: frozenset[str] | set[str] = frozenset(),
    deprecated: frozenset[str] | set[str] = frozenset(),
) -> dict[str, Any]:
    """What ``doctor`` reports about the tool calls of one vault, read from its ``usage_stats.json``.

    For each window of ``windows`` UTC days ending on ``day`` (today by
    default) and for every kept day: calls, errors, and calls by tool, by
    client and by principal, each sorted by name. ``deprecated`` lists the
    calls, over every kept day, to a tool of ``deprecated``; ``unregistered``
    the calls to a name outside ``registered`` (skipped when ``registered``
    is empty). A missing or unreadable file reads as no calls.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        raw = {}
    day = day or utc_day()
    days = _kept_call_days(_tool_call_days(raw.get("tool_calls") if isinstance(raw, dict) else None), day)

    def window(first: str) -> dict[str, Any]:
        calls = errs = 0
        by_tool: dict[str, int] = {}
        by_client: dict[str, int] = {}
        by_principal: dict[str, int] = {}
        for when, tools in days.items():
            if not first <= when <= day:
                continue
            for tool, clients in tools.items():
                for client, principals in clients.items():
                    for principal, leaf in principals.items():
                        calls += leaf["calls"]
                        errs += leaf["errors"]
                        by_tool[tool] = by_tool.get(tool, 0) + leaf["calls"]
                        by_client[client] = by_client.get(client, 0) + leaf["calls"]
                        by_principal[principal] = by_principal.get(principal, 0) + leaf["calls"]
        return {
            "calls": calls,
            "errors": errs,
            "by_tool": dict(sorted(by_tool.items())),
            "by_client": dict(sorted(by_client.items())),
            "by_principal": dict(sorted(by_principal.items())),
        }

    today = date.fromisoformat(day)
    out: dict[str, Any] = {
        "days_kept": TOOL_CALL_DAYS_KEPT,
        "first_day": next(iter(days), None),
        "last_day": next(reversed(days), None) if days else None,
    }
    for size in windows:
        out[f"last_{size}_days"] = window((today - timedelta(days=size - 1)).isoformat())
    kept = window("")
    out["kept"] = kept
    out["deprecated"] = {tool: n for tool, n in kept["by_tool"].items() if tool in deprecated}
    out["unregistered"] = (
        {tool: n for tool, n in kept["by_tool"].items() if tool not in registered} if registered else {}
    )
    return out


def _percentile_ms(histogram: dict[str, int], fraction: float) -> int | None:
    """The bucket bound below which ``fraction`` of the calls fell; ``None`` when none or in the open bucket."""
    total = sum(histogram.values())
    if total <= 0:
        return None
    needed = fraction * total
    seen = 0
    for bound in RERANK_LATENCY_BUCKETS_MS:
        seen += histogram.get(str(bound), 0)
        if seen >= needed:
            return bound
    return None


def rerank_summary(path: Path, *, day: str | None = None) -> dict[str, Any]:
    """What ``doctor`` reports about the judge of one vault, read from its ``usage_stats.json``.

    Aggregates only: calls, the share applied, the share of units served by
    the cache, p50/p95 of the judge latency (bucket bounds), the total cost
    and the cost and searches of ``day`` (today in UTC by default). A missing
    or unreadable file reads as no activity.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        raw = {}
    section = _rerank_section(raw.get("rerank") if isinstance(raw, dict) else None)
    day = day or utc_day()
    calls, units = section["calls"], section["units"]
    today = section["searches_by_day"].get(day, {})
    return {
        "calls": calls,
        "applied_rate": round(section["applied"] / calls, 3) if calls else None,
        "cache_hit_rate": round(section["cache_hits"] / units, 3) if units else None,
        "p50_ms": _percentile_ms(section["latency_ms"], 0.5),
        "p95_ms": _percentile_ms(section["latency_ms"], 0.95),
        "usd_total": round(section["usd"], 6),
        "usd_today": round(section["usd_today"], 6) if section["day"] == day else 0.0,
        "searches_today": today.get("searches", 0),
        "reranked_today": today.get("reranked", 0),
    }


class UsageTracker:
    """Thread-safe retrieval counters with debounced JSON persistence."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._dirty_hits = 0
        # file_path -> {"uses": float, "last_used": iso}
        self._files: dict[str, dict] = {}
        # pair_key -> {week_key: count}
        self._pairs: dict[str, dict[str, int]] = {}
        # Newest-last (monotonic seconds, query, tool, result file paths).
        # In memory only: a search from a previous process explains no open.
        self._recent_queries: deque[tuple[float, str, str, tuple[str, ...]]] = deque(
            maxlen=_RECENT_QUERIES_KEPT
        )
        # Persisted query -> open events, oldest first.
        self._query_opens: list[dict[str, Any]] = []
        # Rerank aggregates; see the module docstring.
        self._rerank: dict[str, Any] = _fresh_rerank()
        # Tool calls this process has not written yet; see the module docstring.
        self._calls_pending: dict[str, dict[str, dict[str, dict[str, dict[str, int]]]]] = {}
        self._decayed_at: float = time.time()
        self._load()

    # -- recording -----------------------------------------------------------

    def record_uses(self, file_paths: list[str], *, weight: float = 1.0) -> None:
        """Count a usage event for each file (get_document should pass weight=2)."""
        if not file_paths:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for fp in file_paths:
                entry = self._files.setdefault(fp, {"uses": 0.0})
                entry["uses"] = float(entry.get("uses", 0.0)) + weight
                entry["last_used"] = now_iso
            self._dirty_hits += len(file_paths)
            self._maybe_flush_locked()

    def record_pairs(self, file_paths: list[str]) -> None:
        """Bucket every pair in a result set into the current ISO week."""
        unique = sorted(set(file_paths))
        if len(unique) < 2:
            return
        week = _week_key()
        with self._lock:
            for a, b in combinations(unique, 2):
                buckets = self._pairs.setdefault(_pair_key(a, b), {})
                buckets[week] = buckets.get(week, 0) + 1
            self._dirty_hits += 1
            self._maybe_flush_locked()

    def record_query(self, query: str, file_paths: list[str], *, tool: str = "search") -> None:
        """Remember a search and the ranked file paths it returned.

        Nothing is persisted here: the search only becomes a record once a
        document it can explain is opened (:meth:`record_open`).
        """
        normalized = " ".join(query.split())
        if not normalized:
            return
        with self._lock:
            self._recent_queries.append((time.monotonic(), normalized, tool, tuple(file_paths)))

    def record_open(self, file_path: str) -> dict[str, Any] | None:
        """Pair a ``get_document`` read with the search that most likely led to it.

        Returns the recorded event, or ``None`` when no recent search exists
        (a document read without searching first says nothing about search).
        """
        now = time.monotonic()
        with self._lock:
            recent = [q for q in self._recent_queries if now - q[0] <= _QUERY_ATTRIBUTION_SECONDS]
            if not recent:
                return None
            chosen = recent[-1]
            rank: int | None = None
            for entry in reversed(recent):
                if file_path in entry[3]:
                    chosen = entry
                    rank = entry[3].index(file_path) + 1
                    break
            event = {
                "query": chosen[1],
                "tool": chosen[2],
                "file_path": file_path,
                "rank": rank,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            self._query_opens.append(event)
            if len(self._query_opens) > _MAX_QUERY_OPENS:
                del self._query_opens[: len(self._query_opens) - _MAX_QUERY_OPENS]
            self._dirty_hits += 1
            self._maybe_flush_locked()
            return dict(event)

    def record_search(self, *, reranked: bool) -> None:
        """Count one ``search`` call in today's (UTC) bucket, and whether the judge reordered it.

        Persisted with the next flush; only the last few weeks are kept.
        """
        day = utc_day()
        with self._lock:
            days = self._rerank["searches_by_day"]
            bucket = days.setdefault(day, {"searches": 0, "reranked": 0})
            bucket["searches"] = bucket.get("searches", 0) + 1
            if reranked:
                bucket["reranked"] = bucket.get("reranked", 0) + 1
            for old in sorted(days)[:-_SEARCH_DAYS_KEPT]:
                del days[old]

    def record_rerank(
        self,
        *,
        applied: bool,
        reason: str | None,
        units: int = 0,
        cache_hits: int = 0,
        input_tokens: int = 0,
        usd: float = 0.0,
        latency_ms: float | None = None,
    ) -> None:
        """Count one search that evaluated the judge: applied, or skipped and why.

        ``units`` were judged in all, ``cache_hits`` of them from the cache;
        ``input_tokens`` and ``usd`` are what the provider billed for the call
        (0 without one) and ``latency_ms`` how long the judge took, when it was
        called. A call that cost something is added to the spend of the day on
        disk and written at once, so another process over the same vault reads
        it before its own next call.
        """
        day = utc_day()
        with self._lock:
            section = self._rerank
            section["calls"] += 1
            if applied:
                section["applied"] += 1
            else:
                key = reason or "unknown"
                section["skipped"][key] = section["skipped"].get(key, 0) + 1
            section["units"] += max(0, units)
            section["cache_hits"] += max(0, cache_hits)
            section["input_tokens"] += max(0, input_tokens)
            section["usd"] += max(0.0, usd)
            if latency_ms is not None:
                bucket = _latency_bucket(latency_ms)
                section["latency_ms"][bucket] = section["latency_ms"].get(bucket, 0) + 1
            if usd > 0:
                with self._file_lock():
                    section["usd_today"] = self._usd_today_locked(day) + usd
                    section["day"] = day
                    self._write_locked()
            else:
                self._dirty_hits += 1
                self._maybe_flush_locked()

    def record_call(self, tool: str, *, client: str | None, principal: str | None, error: bool) -> None:
        """Count one ``tools/call`` of ``tool`` in today's (UTC) bucket of ``client`` and ``principal``.

        The names are reduced to :func:`call_label`; nothing else about the
        call is kept. The count is added onto the file with the next flush.
        """
        day = utc_day()
        name = call_label(tool, max_len=_TOOL_LABEL_MAX)
        with self._lock:
            _add_call(self._calls_pending, day, name, call_label(client), call_label(principal), 1, 1 if error else 0)
            self._dirty_hits += 1
            self._maybe_flush_locked()

    def pending_tool_calls(self) -> dict[str, Any]:
        """A copy of the tool calls this process counted and has not written yet."""
        with self._lock:
            return json.loads(json.dumps(self._calls_pending))

    def rerank_usd_today(self) -> float:
        """US dollars the judge of this vault cost today (UTC), as every process recorded it on disk."""
        with self._lock:
            return self._usd_today_locked(utc_day())

    def _usd_today_locked(self, day: str) -> float:
        own = self._rerank["usd_today"] if self._rerank["day"] == day else 0.0
        disk_day, disk_usd = self._disk_spend()
        shared = disk_usd if disk_day == day else 0.0
        return max(own, shared)

    def _disk_spend(self) -> tuple[str, float]:
        """``(day, usd_today)`` as the usage file holds them; ``("", 0.0)`` when it cannot be read."""
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return "", 0.0
        disk = _rerank_section(raw.get("rerank") if isinstance(raw, dict) else None)
        return disk["day"], disk["usd_today"]

    def _disk_call_days(self) -> dict[str, dict[str, dict[str, dict[str, dict[str, int]]]]]:
        """The ``tool_calls`` days the usage file holds; empty when it cannot be read."""
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}
        return _tool_call_days(raw.get("tool_calls") if isinstance(raw, dict) else None)

    def _merge_disk_spend_locked(self) -> None:
        """Keep the newest day's spend: the larger of this process's and the file's for the same day."""
        disk_day, disk_usd = self._disk_spend()
        if not disk_day:
            return
        section = self._rerank
        if disk_day > section["day"]:
            section["day"], section["usd_today"] = disk_day, disk_usd
        elif disk_day == section["day"]:
            section["usd_today"] = max(section["usd_today"], disk_usd)

    def rerank_stats(self) -> dict[str, Any]:
        """A copy of the rerank aggregates of this process."""
        with self._lock:
            return json.loads(json.dumps(self._rerank))

    # -- signals -------------------------------------------------------------

    def uses(self, file_path: str) -> float:
        return float(self._files.get(file_path, {}).get("uses", 0.0))

    def boost(self, file_path: str, *, beta: float = 0.1) -> float:
        """Multiplier in [1.0, 1+beta], log-scaled against the most-used doc."""
        u = self.uses(file_path)
        if u <= 0:
            return 1.0
        max_uses = max((float(e.get("uses", 0.0)) for e in self._files.values()), default=0.0)
        if max_uses <= 0:
            return 1.0
        return 1.0 + beta * math.log1p(u) / math.log1p(max_uses)

    def co_occurrence(self, a: str, b: str) -> int:
        """Co-retrieval count for the pair within the recent window."""
        buckets = self._pairs.get(_pair_key(a, b))
        if not buckets:
            return 0
        recent = _recent_week_keys()
        return sum(count for week, count in buckets.items() if week in recent)

    def recent_pairs(self) -> dict[str, int]:
        """All pairs with non-zero co-occurrence in the recent window."""
        recent = _recent_week_keys()
        out: dict[str, int] = {}
        for key, buckets in self._pairs.items():
            total = sum(count for week, count in buckets.items() if week in recent)
            if total > 0:
                out[key] = total
        return out

    def query_opens(self) -> list[dict[str, Any]]:
        """Every persisted query -> open event, oldest first (copies)."""
        with self._lock:
            return [dict(e) for e in self._query_opens]

    # -- persistence ---------------------------------------------------------

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def flush_if_dirty(self) -> None:
        """Write the file only when something was recorded since the last write."""
        with self._lock:
            if self._dirty_hits or self._calls_pending:
                self._flush_locked()

    def _maybe_flush_locked(self) -> None:
        if self._dirty_hits >= _FLUSH_EVERY_HITS:
            self._flush_locked()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
            self._files = raw.get("files", {})
            self._pairs = raw.get("pairs", {})
            self._query_opens = list(raw.get("query_opens", []))[-_MAX_QUERY_OPENS:]
            self._rerank = _rerank_section(raw.get("rerank"))
            self._decayed_at = float(raw.get("decayed_at", time.time()))
        except FileNotFoundError:
            return
        except Exception:
            log.exception("usage: failed to load %s — starting fresh", self._path)
            return
        self._apply_decay()

    def _apply_decay(self) -> None:
        now = time.time()
        elapsed_days = (now - self._decayed_at) / 86400
        if elapsed_days < 1:
            return
        factor = 0.5 ** (elapsed_days / _HALF_LIFE_DAYS)
        for entry in self._files.values():
            entry["uses"] = float(entry.get("uses", 0.0)) * factor
        self._files = {
            fp: e for fp, e in self._files.items() if float(e.get("uses", 0.0)) >= _MIN_USES_KEPT
        }
        # prune pair buckets that fell out of the window
        recent = _recent_week_keys()
        for key in list(self._pairs):
            kept = {w: c for w, c in self._pairs[key].items() if w in recent}
            if kept:
                self._pairs[key] = kept
            else:
                del self._pairs[key]
        self._decayed_at = now

    @contextlib.contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Exclusive lock between processes over the usage file, held on a ``.lock`` sidecar.

        Serialises the read-merge-write of the shared daily spend. When the
        sidecar cannot be opened or locked the block still runs, unlocked.
        """
        if fcntl is None:
            yield
            return
        lock_path = self._path.with_name(self._path.name + ".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a")  # noqa: SIM115 - closed in the finally below
        except OSError:
            log.debug("usage: cannot open %s, writing without the file lock", lock_path)
            yield
            return
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                log.debug("usage: cannot lock %s, writing without the file lock", lock_path)
            yield
        finally:
            handle.close()

    def _flush_locked(self) -> None:
        with self._file_lock():
            self._merge_disk_spend_locked()
            self._write_locked()

    def _write_locked(self) -> None:
        """Serialise the state and replace the file atomically; the caller holds both locks."""
        self._apply_decay()
        payload = {
            "decayed_at": self._decayed_at,
            "files": self._files,
            "pairs": self._pairs,
            "query_opens": self._query_opens,
        }
        if self._rerank != _fresh_rerank():
            payload["rerank"] = self._rerank
        calls = self._disk_call_days()
        _merge_call_days(calls, self._calls_pending)
        calls = _kept_call_days(calls, utc_day())
        if calls:
            payload["tool_calls"] = {"days": calls}
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False))
            os.replace(tmp, self._path)
            self._dirty_hits = 0
            self._calls_pending = {}
        except Exception:
            log.exception("usage: failed to flush %s", self._path)
            with contextlib.suppress(OSError):
                tmp.unlink()
