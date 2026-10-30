"""Rerank of ``search`` with the judge a vault declares: policy and orchestration, no network.

``search`` asks this module whether to rerank and, when it does, hands it the
hybrid pool. Nothing here speaks to a provider: the :class:`~knowledge.ports.Judge`
arrives built (``knowledge.judges`` builds it for a vault that declares one)
and the idf of the query terms arrives computed (the server reads it from the
keyword index).

What is judged is what a reader gets: the **pointer blocks** of a document
(the text ``get_document(chunk_id=…)`` returns), cut to
:data:`~knowledge.pointers.READ_WINDOW_CHARS` characters, up to
``rerank.units_per_doc`` per document over the first ``rerank.candidates``
documents of the pool. The first block of a document is the one whose heading
is the section of its best dense chunk; the rest are the blocks the query
terms hit hardest. All of them go in one request.

The fused score is ``round(hybrid · boost · (0.8 + 0.5 · p), 2)``, where ``p``
is the best probability among the document's blocks: a bounded multiplier in
[0.8, 1.3] that reorders close calls and never replaces the hybrid ranking. A
document the judge did not see keeps its score (multiplier 1). The block with
the best ``p`` is the one the row points at.

A judge that cannot answer never fails ``search``: the caller gets the
ranking without rerank and a ``reason``. Every reason is one of
:data:`REASONS`. The state that outlives a call lives in one
:class:`Reranker` per vault: an LRU cache of judged blocks with a TTL, keyed
by the query and the block (its ``chunk_id`` and a hash of its text, so an
edit invalidates it), and a circuit breaker. The daily spending cap is read
from the vault's usage file, which every process over the vault shares.

A core module: it imports ports, settings, pointers and usage, nothing that
speaks to the network or to MCP.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from knowledge import pointers
from knowledge.ports import Judge, JudgeUnavailable, JudgeUnit, NullJudge
from knowledge.settings import RerankSettings
from knowledge.usage import UsageTracker

log = logging.getLogger(__name__)

#: Values of the ``rerank`` argument of ``search``; ``None`` (omitted) follows the vault.
RERANK_ON = "on"
RERANK_OFF = "off"
RERANK_ARGUMENTS: tuple[str, ...] = (RERANK_ON, RERANK_OFF)

#: Why a search that evaluated rerank did not apply it. ``_meta`` carries one of them.
REASON_OFF = "off"
REASON_TIMEOUT = "timeout"
REASON_UNAVAILABLE = "unavailable"
REASON_AUTH = "auth"
REASON_INVALID = "invalid_response"
REASON_BREAKER = "breaker_open"
REASON_BUDGET_TOKENS = "budget_tokens"
REASON_BUDGET_DAY = "budget_day"
REASON_MEMORIES = "memories_included"
REASON_GAP = "gap"
REASON_NO_CANDIDATES = "no_candidates"
REASON_DISABLED = "disabled"
REASON_KEY_MISSING = "key_missing"
REASON_MODE = "mode"
REASONS: tuple[str, ...] = (
    REASON_OFF,
    REASON_TIMEOUT,
    REASON_UNAVAILABLE,
    REASON_AUTH,
    REASON_INVALID,
    REASON_BREAKER,
    REASON_BUDGET_TOKENS,
    REASON_BUDGET_DAY,
    REASON_MEMORIES,
    REASON_GAP,
    REASON_NO_CANDIDATES,
    REASON_DISABLED,
    REASON_KEY_MISSING,
    REASON_MODE,
)
#: Judge failures that count against the breaker (``auth`` opens it at once).
_FAILURES = frozenset({REASON_TIMEOUT, REASON_UNAVAILABLE, REASON_AUTH, REASON_INVALID})

#: Documents the pool holds at least: the judge sees its first ``candidates``
#: and fusion may lift any of them into the first ``limit``. The pool is a
#: hybrid search with this limit (and twice as many chunks fetched per path).
POOL_MIN = 20
#: Characters of a block a judge reads: what the reading protocol passes to ``get_document``.
UNIT_CHARS = pointers.READ_WINDOW_CHARS
#: Token estimate of one request: ``ceil(chars / CHARS_PER_TOKEN) + TOKENS_PER_UNIT · units + TOKENS_PER_REQUEST``
#: over the compact JSON of ``state`` without ASCII escapes. Calibrated against
#: what Jev billed: the estimate stays between 1.00 and 1.12 times the bill.
CHARS_PER_TOKEN = 2.95
TOKENS_PER_UNIT = 50
TOKENS_PER_REQUEST = 300
#: The bounded multiplier of the fused score: ``FLOOR + SPAN · p``, from 0.8 to 1.3.
MULTIPLIER_FLOOR = 0.8
MULTIPLIER_SPAN = 0.5
#: Judged blocks kept per vault, and for how long.
CACHE_SIZE = 4096
CACHE_TTL_S = 7 * 24 * 3600
#: Consecutive failures that open the breaker, and for how long it stays open.
BREAKER_FAILURES = 3
BREAKER_COOLDOWN_S = 60.0
#: How long a refused key keeps the breaker open.
AUTH_COOLDOWN_S = 15 * 60.0


def pool_limit(limit: int) -> int:
    """Documents in the pool of a reranked search that returns ``limit`` rows."""
    return max(limit, POOL_MIN)


def document_name(file_path: str) -> str:
    """The name a judge reads for a document: its file stem."""
    return PurePosixPath(file_path).stem


@dataclass(frozen=True)
class Candidate:
    """One document of the pool the judge may read.

    ``raw`` is its hybrid score times the usage boost, before rounding;
    ``dense_section`` the heading of its best chunk (``""`` when none) and
    ``body`` its Markdown without frontmatter (``None`` when the file is gone).
    """

    file_path: str
    raw: float
    dense_section: str
    body: str | None


@dataclass(frozen=True)
class Unit:
    """One pointer block of a candidate, as the judge reads it.

    ``key`` is the block's ``chunk_id``; ``section`` its heading (``None`` for
    the preamble) and ``text`` the block cut to :data:`UNIT_CHARS`.
    """

    file_path: str
    ordinal: int
    section: str | None
    text: str
    key: str

    def judge_unit(self) -> JudgeUnit:
        return JudgeUnit(
            key=self.key, document=document_name(self.file_path), section=self.section or "", text=self.text
        )


def _excluded(file_path: str, prefixes: Sequence[str]) -> bool:
    return any(prefix and file_path.startswith(prefix) for prefix in prefixes)


def units_for(
    candidates: Sequence[Candidate],
    idf: Mapping[str, float],
    *,
    units_per_doc: int,
    never_send: Sequence[str] = (),
) -> list[Unit]:
    """The blocks of ``candidates`` a judge reads, document by document, in pool order.

    Per document, up to ``units_per_doc`` distinct blocks: first the block
    whose heading is the section of its best dense chunk, then the blocks the
    query terms in ``idf`` hit hardest within the read window (ties to the
    longer window, then the earlier block). A document under a ``never_send``
    prefix, or whose file is gone, contributes nothing.
    """
    units: list[Unit] = []
    for candidate in candidates:
        if candidate.body is None or _excluded(candidate.file_path, never_send):
            continue
        body = candidate.body
        heads = pointers.headings(body)
        blocks = [(ordinal, pointers.block(body, ordinal, heads)) for ordinal in range(len(heads) + 1)]
        blocks = [(ordinal, text) for ordinal, text in blocks if text]
        if not blocks:
            continue
        ranked = sorted(
            blocks,
            key=lambda item: (
                -pointers.window_score(item[1], idf, window=UNIT_CHARS),
                -min(len(item[1]), UNIT_CHARS),
                item[0],
            ),
        )
        wanted = candidate.dense_section.strip()
        picks = [o for o, _ in blocks if wanted and o and heads[o - 1].text.strip() == wanted][:1]
        for ordinal, _ in ranked:
            if len(picks) >= units_per_doc:
                break
            if ordinal not in picks:
                picks.append(ordinal)
        text_of = dict(blocks)
        doc = pointers.doc_id(candidate.file_path)
        for ordinal in picks[:units_per_doc]:
            units.append(
                Unit(
                    file_path=candidate.file_path,
                    ordinal=ordinal,
                    section=heads[ordinal - 1].text if ordinal else None,
                    text=text_of[ordinal][:UNIT_CHARS],
                    key=pointers.chunk_id(doc, ordinal),
                )
            )
    return units


def request_state(query: str, units: Sequence[Unit]) -> dict[str, Any]:
    """The ``state`` a System One request carries for ``units``."""
    return {
        "query": query,
        "candidates": [
            {"document": document_name(u.file_path), "section": u.section or "", "text": u.text} for u in units
        ],
    }


def estimate_tokens_from(chars: int, units: int) -> int:
    """Billed input tokens estimated from the characters of ``state`` and the number of units."""
    if units <= 0:
        return 0
    return math.ceil(chars / CHARS_PER_TOKEN) + TOKENS_PER_UNIT * units + TOKENS_PER_REQUEST


def estimate_tokens(query: str, units: Sequence[Unit]) -> int:
    """Billed input tokens a request for ``units`` is estimated to cost (0 for none)."""
    chars = len(json.dumps(request_state(query, units), ensure_ascii=False, separators=(",", ":")))
    return estimate_tokens_from(chars, len(units))


def fit_budget(query: str, units: Sequence[Unit], max_tokens: int) -> list[Unit]:
    """``units`` cut until a request for them is estimated at ``max_tokens`` or less.

    The extra blocks go first, from the bottom document up; then whole
    documents, from the bottom up. Empty when not even one block fits.
    """
    kept = list(units)
    while kept and estimate_tokens(query, kept) > max_tokens:
        order = list(dict.fromkeys(u.file_path for u in kept))
        crowded = next((fp for fp in reversed(order) if sum(1 for u in kept if u.file_path == fp) > 1), None)
        if crowded is not None:
            last = max(i for i, u in enumerate(kept) if u.file_path == crowded)
            del kept[last]
        else:
            drop = order[-1]
            kept = [u for u in kept if u.file_path != drop]
    return kept


def _normalized(query: str) -> str:
    return " ".join(query.lower().split())


@dataclass
class _Breaker:
    failures: int = 0
    open_until: float = 0.0

    def is_open(self, now: float) -> bool:
        return now < self.open_until

    def success(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def failure(self, reason: str, now: float) -> None:
        if reason == REASON_AUTH:
            self.failures = 0
            self.open_until = now + AUTH_COOLDOWN_S
            return
        self.failures += 1
        if self.failures >= BREAKER_FAILURES:
            self.failures = 0
            self.open_until = now + BREAKER_COOLDOWN_S


@dataclass
class Outcome:
    """What the rerank of one search did.

    ``rows`` are the final rows (fused, sorted, cut to ``limit``) when
    ``applied``; ``chosen`` maps each judged document to the ordinal of the
    block the judge preferred. ``candidates`` documents and ``units`` blocks
    were prepared for the judge, ``cache_hits`` of them answered from the
    cache. ``input_tokens``, ``usd`` and ``latency_ms`` describe the provider
    call, when one ran.
    """

    applied: bool
    reason: str | None
    rows: list[dict[str, Any]] | None = None
    chosen: dict[str, int] = field(default_factory=dict)
    candidates: int = 0
    units: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    usd: float = 0.0
    latency_ms: float | None = None

    @classmethod
    def skipped(cls, reason: str, **counts: Any) -> Outcome:
        return cls(applied=False, reason=reason, **counts)


def meta(spec: RerankSettings, outcome: Outcome) -> dict[str, Any]:
    """``_meta["knowledge/rerank"]`` of a search: deterministic, no latency and no text."""
    return {
        "provider": spec.provider,
        "model": spec.model,
        "applied": outcome.applied,
        "reason": outcome.reason,
        "candidates": outcome.candidates,
        "units": outcome.units,
        "cache_hits": outcome.cache_hits,
    }


def requested(argument: str | None, spec: RerankSettings) -> str | None:
    """What a ``search`` call asks of the judge: ``on``, ``off`` or ``None`` (rerank is not evaluated).

    ``argument`` is the already validated ``rerank`` argument; omitted, it
    follows ``rerank.default`` of the vault.
    """
    if argument is not None:
        return argument
    return RERANK_ON if spec.configured and spec.default_on else None


def precheck(
    spec: RerankSettings, *, allowed: bool, judge: Judge, mode: str, include_memories: bool
) -> str | None:
    """Why a search that asks for rerank cannot have it before any retrieval; ``None`` when it can.

    ``allowed`` is whether the operator lets this provider run
    (``KNOWLEDGE_RERANK``, ``KNOWLEDGE_RERANK_PROVIDERS``). A vault without a
    working judge reads as ``key_missing`` when its key variable is empty.
    Only hybrid search is reranked, and memories never reach a judge.
    """
    if not allowed:
        return REASON_DISABLED
    if isinstance(judge, NullJudge) or judge.name == "none":
        return REASON_KEY_MISSING if not spec.api_key else REASON_UNAVAILABLE
    if mode in ("semantic", "keyword"):
        return REASON_MODE
    if include_memories:
        return REASON_MEMORIES
    return None


def fused_score(raw: float, probability: float) -> float:
    """The score of a judged row: the hybrid score times the bounded multiplier, 2 decimals."""
    return round(raw * (MULTIPLIER_FLOOR + MULTIPLIER_SPAN * probability), 2)


class Reranker:
    """Rerank state of one vault: the cache of judged blocks and the circuit breaker.

    Safe to share between threads: the cache and the breaker sit behind a
    lock that is never held while the judge runs, so concurrent searches
    never queue behind one another's provider call. ``clock`` (wall seconds,
    for the cache TTL) and ``monotonic`` (for the breaker) are injectable.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        cache_size: int = CACHE_SIZE,
        cache_ttl_s: float = CACHE_TTL_S,
    ) -> None:
        self._clock = clock
        self._monotonic = monotonic
        self._cache_size = cache_size
        self._cache_ttl_s = cache_ttl_s
        self._cache: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._breaker = _Breaker()
        self._lock = threading.Lock()

    # -- state ------------------------------------------------------------------

    def breaker_open(self) -> bool:
        with self._lock:
            return self._breaker.is_open(self._monotonic())

    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    @staticmethod
    def cache_key(spec: RerankSettings, prompt_version: str, query: str, unit: Unit) -> str:
        """Key of one verdict in the in-process cache.

        Internal only: the key never leaves the process, never reaches a
        response or ``_meta`` and is not part of the tool contract, so its
        digest can change without a contract change.
        """
        text_hash = hashlib.sha256(unit.text.encode("utf-8")).hexdigest()
        raw = "\x1f".join((spec.provider, spec.model, prompt_version, _normalized(query), unit.key, text_hash))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cached(self, key: str, now: float) -> float | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        probability, stored_at = entry
        if now - stored_at > self._cache_ttl_s:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return probability

    def _store(self, key: str, probability: float, now: float) -> None:
        self._cache[key] = (probability, now)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    # -- the rerank -------------------------------------------------------------

    def run(
        self,
        query: str,
        rows: Sequence[dict[str, Any]],
        candidates: Sequence[Candidate],
        *,
        judge: Judge,
        spec: RerankSettings,
        idf: Mapping[str, float],
        limit: int,
        timeout_s: float,
        cost: Callable[[str, int], float],
        usage: UsageTracker | None = None,
        prompt_version: str = "",
    ) -> Outcome:
        """Rerank the pool ``rows`` (hybrid order) with ``judge`` over ``candidates``.

        ``candidates`` are the first ``rerank.candidates`` documents of the
        pool, with their raw score and body. ``cost(model, input_tokens)``
        prices a call in US dollars; ``usage`` holds the vault's spend of the
        day. Never raises for a judge that cannot answer: the outcome carries
        the reason instead.
        """
        if spec.conditional_gap is not None and len(candidates) >= 2:
            if candidates[0].raw - candidates[1].raw >= spec.conditional_gap:
                return Outcome.skipped(REASON_GAP)
        units = units_for(candidates, idf, units_per_doc=spec.units_per_doc, never_send=spec.never_send)
        if not units:
            return Outcome.skipped(REASON_NO_CANDIDATES)
        units = fit_budget(query, units, spec.max_input_tokens)
        if not units:
            return Outcome.skipped(REASON_BUDGET_TOKENS)
        documents = len({u.file_path for u in units})
        keys = {u.key: self.cache_key(spec, prompt_version, query, u) for u in units}

        scores: dict[str, float] = {}
        with self._lock:
            now = self._clock()
            for unit in units:
                hit = self._cached(keys[unit.key], now)
                if hit is not None:
                    scores[unit.key] = hit
        cache_hits = len(scores)
        missing = [u for u in units if u.key not in scores]
        counts = {"candidates": documents, "units": len(units), "cache_hits": cache_hits}
        input_tokens, usd, latency_ms = 0, 0.0, None

        if missing:
            if self.breaker_open():
                return Outcome.skipped(REASON_BREAKER, **counts)
            if spec.max_usd_per_day > 0 and usage is not None:
                estimate = cost(spec.model, estimate_tokens(query, missing))
                if usage.rerank_usd_today() + estimate > spec.max_usd_per_day:
                    return Outcome.skipped(REASON_BUDGET_DAY, **counts)
            started = time.perf_counter()
            try:
                verdict = judge.judge(query, [u.judge_unit() for u in missing], timeout_s=timeout_s)
                if any(u.key not in verdict.scores for u in missing):
                    raise JudgeUnavailable(REASON_INVALID, "the judge left a unit unscored")
            except Exception as exc:  # noqa: BLE001 - a judge never fails the search
                latency_ms = (time.perf_counter() - started) * 1000
                if isinstance(exc, JudgeUnavailable) and exc.reason in _FAILURES:
                    reason = exc.reason
                else:
                    reason = REASON_UNAVAILABLE
                    if not isinstance(exc, JudgeUnavailable):
                        log.warning("rerank: the %s judge failed with %s", judge.name, type(exc).__name__)
                with self._lock:
                    self._breaker.failure(reason, self._monotonic())
                return Outcome.skipped(reason, latency_ms=latency_ms, **counts)
            latency_ms = (time.perf_counter() - started) * 1000
            input_tokens = verdict.input_tokens
            usd = cost(spec.model, input_tokens)
            with self._lock:
                self._breaker.success()
                now = self._clock()
                for unit in missing:
                    probability = min(1.0, max(0.0, float(verdict.scores[unit.key])))
                    scores[unit.key] = probability
                    self._store(keys[unit.key], probability, now)

        best: dict[str, tuple[float, int]] = {}
        for unit in units:
            probability = scores[unit.key]
            current = best.get(unit.file_path)
            if current is None or probability > current[0]:
                best[unit.file_path] = (probability, unit.ordinal)
        raw = {c.file_path: c.raw for c in candidates}
        fused: list[dict[str, Any]] = []
        for row in rows:
            file_path = row["file_path"]
            if file_path in best:
                probability = best[file_path][0]
                score = fused_score(raw[file_path], probability)
                fused.append({**row, "score": score, "rerank_score": round(probability, 2)})
            else:
                fused.append(dict(row))
        fused.sort(key=lambda row: (-row["score"], row["file_path"]))
        final = fused[:limit]
        chosen = {row["file_path"]: best[row["file_path"]][1] for row in final if row["file_path"] in best}
        return Outcome(
            applied=True,
            reason=None,
            rows=final,
            chosen=chosen,
            input_tokens=input_tokens,
            usd=usd,
            latency_ms=latency_ms,
            **counts,
        )
