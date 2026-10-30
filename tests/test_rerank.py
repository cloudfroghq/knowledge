"""The rerank of ``search`` in the core: units, budget, cache, breaker, fusion and policy.

Everything here runs in process against :class:`FakeJudge`, a judge that
scores from a table and records what it was asked; nothing reaches a socket.
The end-to-end path through the server and the System One adapter is
``tests/test_search_rerank.py``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from knowledge import pointers, rerank
from knowledge.ports import JudgeUnavailable, JudgeUnit, JudgeVerdict, NullJudge
from knowledge.settings import RerankSettings
from knowledge.usage import UsageTracker, rerank_summary

from systemone_fakes import billed_tokens

FIXTURES = Path(__file__).parent / "fixtures" / "systemone"

SPEC = RerankSettings(provider="jev", model="jev-1.13.0", key_env="TYPESAFE_API_KEY", api_key="k", candidates=3)


@dataclass
class FakeJudge:
    """Scores each unit from ``table`` (by ``chunk_id``; ``default`` otherwise) and records every call."""

    table: Mapping[str, float] = field(default_factory=dict)
    default: float = 0.5
    fail: str | None = None
    name: str = "jev"
    model: str = "jev-1.13.0"
    calls: list[list[JudgeUnit]] = field(default_factory=list)

    def judge(self, query: str, units: Sequence[JudgeUnit], *, timeout_s: float) -> JudgeVerdict:
        del query, timeout_s
        self.calls.append(list(units))
        if self.fail is not None:
            raise JudgeUnavailable(self.fail)
        scores = {u.key: self.table.get(u.key, self.default) for u in units}
        return JudgeVerdict(scores=scores, input_tokens=1000 * len(units), model=self.model)


def _cost(model: str, tokens: int) -> float:
    del model
    return tokens * 0.042 / 1_000_000


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _doc(*sections: tuple[str, str], preamble: str = "") -> str:
    parts = [preamble] if preamble else []
    parts += [f"## {heading}\n\n{text}" for heading, text in sections]
    return "\n\n".join(parts) + "\n"


def _pool(n: int = 4) -> tuple[list[dict], list[rerank.Candidate]]:
    """``n`` documents with two sections each, ranked by a falling hybrid score."""
    rows, candidates = [], []
    for i in range(n):
        fp = f"Docs/Doc {i}.md"
        raw = 0.9 - 0.1 * i
        rows.append({"file_path": fp, "title": f"Doc {i}", "score": round(raw, 2), "excerpt": "x"})
        body = _doc(("Alpha", f"alpha text of doc {i} about outages"), ("Beta", f"beta text of doc {i} about outages"))
        candidates.append(rerank.Candidate(file_path=fp, raw=raw, dense_section="Beta", body=body))
    return rows, candidates


def _chunk(fp: str, ordinal: int) -> str:
    return pointers.chunk_id(pointers.doc_id(fp), ordinal)


def _run(reranker: rerank.Reranker, judge, *, spec: RerankSettings = SPEC, limit: int = 3, usage=None, query="outage"):
    rows, candidates = _pool()
    return reranker.run(
        query,
        rows,
        candidates[: spec.candidates],
        judge=judge,
        spec=spec,
        idf={"outages": 1.0, "alpha": 0.5},
        limit=limit,
        timeout_s=1.5,
        cost=_cost,
        usage=usage,
        prompt_version="rr-noul-v1",
    )


# --- units -----------------------------------------------------------------------


def test_units_take_the_dense_section_first_then_the_hardest_hit_block() -> None:
    body = _doc(("Alpha", "outage outage outage alpha"), ("Beta", "nothing here"), preamble="intro")
    candidate = rerank.Candidate(file_path="A/Doc.md", raw=0.5, dense_section="Beta", body=body)

    units = rerank.units_for([candidate], {"outage": 1.0}, units_per_doc=2)

    assert [(u.ordinal, u.section) for u in units] == [(2, "Beta"), (1, "Alpha")]
    assert [u.key for u in units] == [_chunk("A/Doc.md", 2), _chunk("A/Doc.md", 1)]
    assert units[0].judge_unit() == JudgeUnit(key=units[0].key, document="Doc", section="Beta", text="## Beta\n\nnothing here")


def test_units_never_repeat_a_block_and_stop_at_units_per_doc() -> None:
    body = _doc(("Alpha", "outage"), ("Beta", "outage outage"), ("Gamma", "outage outage outage"))
    candidate = rerank.Candidate(file_path="A.md", raw=0.5, dense_section="Gamma", body=body)

    one = rerank.units_for([candidate], {"outage": 1.0}, units_per_doc=1)
    three = rerank.units_for([candidate], {"outage": 1.0}, units_per_doc=3)

    assert [u.ordinal for u in one] == [3]
    assert [u.ordinal for u in three] == [3, 2, 1]


def test_units_are_cut_at_the_read_window() -> None:
    body = _doc(("Long", "word " * 1000))
    candidate = rerank.Candidate(file_path="A.md", raw=0.5, dense_section="", body=body)

    (unit,) = rerank.units_for([candidate], {}, units_per_doc=1)

    assert len(unit.text) == pointers.READ_WINDOW_CHARS == rerank.UNIT_CHARS


def test_never_send_and_missing_files_contribute_nothing() -> None:
    body = _doc(("Alpha", "outage"))
    candidates = [
        rerank.Candidate(file_path="Personal/Diary.md", raw=0.9, dense_section="", body=body),
        rerank.Candidate(file_path="Gone.md", raw=0.8, dense_section="", body=None),
        rerank.Candidate(file_path="Kept.md", raw=0.7, dense_section="", body=body),
    ]

    units = rerank.units_for(candidates, {"outage": 1.0}, units_per_doc=2, never_send=("Personal/",))

    assert {u.file_path for u in units} == {"Kept.md"}


# --- tokens ----------------------------------------------------------------------


def test_the_estimate_is_the_number_the_loopback_double_bills() -> None:
    _, candidates = _pool()
    units = rerank.units_for(candidates, {"outages": 1.0}, units_per_doc=2)
    state = rerank.request_state("¿cómo se resuelve un outage?", units)

    assert rerank.estimate_tokens("¿cómo se resuelve un outage?", units) == billed_tokens(state, len(units))
    assert rerank.estimate_tokens("q", []) == 0


def test_the_budget_drops_extra_blocks_from_the_bottom_then_whole_documents() -> None:
    _, candidates = _pool()
    units = rerank.units_for(candidates, {"outages": 1.0}, units_per_doc=2)
    assert len(units) == 8
    one_less = rerank.estimate_tokens("q", units[:-1])

    trimmed = rerank.fit_budget("q", units, one_less)
    assert trimmed == units[:-1]

    firsts = [u for i, u in enumerate(units) if i % 2 == 0]
    only_firsts = rerank.fit_budget("q", units, rerank.estimate_tokens("q", firsts))
    assert only_firsts == firsts

    two_docs = rerank.fit_budget("q", units, rerank.estimate_tokens("q", firsts[:2]))
    assert two_docs == firsts[:2]
    assert rerank.fit_budget("q", units, 10) == []


def test_no_query_of_the_evaluation_set_is_trimmed_by_the_default_budget() -> None:
    """The pool the pilot judged (10 documents, up to 2 blocks each) fits 20,000 tokens for all 50 queries.

    The fixture keeps only sizes: per query, the characters of the compact
    ``state`` and the number of blocks, measured over the evaluation corpus
    with :func:`knowledge.rerank.request_state`. No vault content lives here.
    """
    data = json.loads((FIXTURES / "eval-budget.json").read_text(encoding="utf-8"))
    estimates = [rerank.estimate_tokens_from(q["state_chars"], q["units"]) for q in data["queries"]]

    assert len(estimates) == 50
    assert RerankSettings().max_input_tokens == 20_000
    assert max(estimates) <= 20_000, max(estimates)


# --- fusion ----------------------------------------------------------------------


def test_fusion_multiplies_the_hybrid_score_within_bounds_and_reorders() -> None:
    rows, candidates = _pool()
    judge = FakeJudge(table={_chunk(candidates[2].file_path, 1): 1.0}, default=0.0)

    outcome = _run(rerank.Reranker(), judge, limit=4)

    assert outcome.applied and outcome.reason is None
    assert outcome.rows is not None
    by_path = {row["file_path"]: row for row in outcome.rows}
    assert by_path[candidates[2].file_path]["score"] == rerank.fused_score(0.7, 1.0) == round(0.7 * 1.3, 2)
    assert by_path[candidates[0].file_path]["score"] == round(0.9 * 0.8, 2)
    assert outcome.rows[0]["file_path"] == candidates[2].file_path
    assert outcome.chosen[candidates[2].file_path] == 1
    # The fourth document was not judged (candidates=3): no rerank_score, its score untouched.
    assert "rerank_score" not in by_path[candidates[3].file_path]
    assert by_path[candidates[3].file_path]["score"] == rows[3]["score"]
    assert all(row["rerank_score"] in (0.0, 1.0) for row in outcome.rows if "rerank_score" in row)
    assert (outcome.candidates, outcome.units, outcome.cache_hits) == (3, 6, 0)


def test_the_best_block_of_a_document_is_its_probability_and_its_pointer() -> None:
    _, candidates = _pool()
    fp = candidates[0].file_path
    judge = FakeJudge(table={_chunk(fp, 2): 0.2, _chunk(fp, 1): 0.9}, default=0.0)

    outcome = _run(rerank.Reranker(), judge)

    row = next(r for r in outcome.rows or [] if r["file_path"] == fp)
    assert row["rerank_score"] == 0.9
    assert outcome.chosen[fp] == 1


def test_rows_are_cut_to_the_limit_after_fusion() -> None:
    outcome = _run(rerank.Reranker(), FakeJudge(), limit=2)

    assert outcome.rows is not None and len(outcome.rows) == 2


# --- cache -----------------------------------------------------------------------


def test_an_identical_search_is_answered_from_the_cache() -> None:
    reranker, judge = rerank.Reranker(), FakeJudge()

    first = _run(reranker, judge)
    second = _run(reranker, judge)

    assert len(judge.calls) == 1
    assert (second.cache_hits, second.units) == (first.units, first.units)
    assert second.rows == first.rows


def test_a_partial_hit_sends_only_the_missing_units() -> None:
    reranker, judge = rerank.Reranker(), FakeJudge()
    _run(reranker, judge, spec=replace(SPEC, candidates=2))

    outcome = _run(reranker, judge, spec=replace(SPEC, candidates=3))

    assert len(judge.calls) == 2
    _, candidates = _pool()
    assert {u.key for u in judge.calls[1]} == {_chunk(candidates[2].file_path, 2), _chunk(candidates[2].file_path, 1)}
    assert outcome.cache_hits == 4 and outcome.units == 6


def test_the_cache_expires_after_its_ttl_and_on_a_changed_text() -> None:
    clock = Clock()
    reranker, judge = rerank.Reranker(clock=clock), FakeJudge()
    _run(reranker, judge)

    clock.now += rerank.CACHE_TTL_S - 1
    _run(reranker, judge)
    assert len(judge.calls) == 1

    clock.now += 2
    _run(reranker, judge)
    assert len(judge.calls) == 2

    _run(reranker, judge, query="another outage")
    assert len(judge.calls) == 3


def test_the_cache_keeps_at_most_its_size() -> None:
    reranker = rerank.Reranker(cache_size=4)
    _run(reranker, FakeJudge())

    assert reranker.cache_size() == 4


# --- breaker ---------------------------------------------------------------------


def test_three_failures_open_the_breaker_until_the_cooldown_passes() -> None:
    clock = Clock()
    reranker, judge = rerank.Reranker(monotonic=clock), FakeJudge(fail="timeout")

    reasons = [_run(reranker, judge, query=f"q{i}").reason for i in range(4)]

    assert reasons == ["timeout", "timeout", "timeout", "breaker_open"]
    assert len(judge.calls) == 3
    clock.now += rerank.BREAKER_COOLDOWN_S + 1
    judge.fail = None
    assert _run(reranker, judge, query="q9").applied
    assert not reranker.breaker_open()


def test_a_refused_key_opens_the_breaker_for_fifteen_minutes() -> None:
    clock = Clock()
    reranker, judge = rerank.Reranker(monotonic=clock), FakeJudge(fail="auth")

    assert _run(reranker, judge).reason == "auth"
    clock.now += rerank.BREAKER_COOLDOWN_S + 1
    assert _run(reranker, judge, query="other").reason == "breaker_open"
    clock.now += rerank.AUTH_COOLDOWN_S
    assert _run(reranker, judge, query="third").reason == "auth"
    assert len(judge.calls) == 2


def test_a_verdict_that_leaves_a_unit_unscored_is_an_invalid_response() -> None:
    class Partial(FakeJudge):
        def judge(self, query, units, *, timeout_s):
            verdict = super().judge(query, units, timeout_s=timeout_s)
            return JudgeVerdict(scores=dict(list(verdict.scores.items())[:-1]), input_tokens=1)

    assert _run(rerank.Reranker(), Partial()).reason == "invalid_response"


def test_a_judge_that_raises_anything_else_is_unavailable_never_an_error() -> None:
    class Broken(FakeJudge):
        def judge(self, query, units, *, timeout_s):
            raise OSError(9, "Bad file descriptor")

    outcome = _run(rerank.Reranker(), Broken())

    assert (outcome.applied, outcome.reason) == (False, "unavailable")


def test_cached_units_answer_even_while_the_breaker_is_open() -> None:
    clock = Clock()
    reranker, judge = rerank.Reranker(monotonic=clock), FakeJudge()
    _run(reranker, judge)
    judge.fail = "unavailable"
    for i in range(3):
        _run(reranker, judge, query=f"miss {i}")
    assert reranker.breaker_open()

    assert _run(reranker, judge).applied


# --- budgets and policy ----------------------------------------------------------


def test_a_tiny_token_budget_skips_the_judge() -> None:
    judge = FakeJudge()

    outcome = _run(rerank.Reranker(), judge, spec=replace(SPEC, max_input_tokens=50))

    assert outcome.reason == "budget_tokens" and judge.calls == []


def test_the_daily_cap_is_shared_by_every_process_over_the_usage_file(tmp_path: Path) -> None:
    """Two trackers over one ``usage_stats.json`` stand for two stdio processes over one vault."""
    path = tmp_path / "usage_stats.json"
    first, second = UsageTracker(path), UsageTracker(path)
    first.record_rerank(applied=True, reason=None, units=6, input_tokens=6000, usd=0.01)
    judge = FakeJudge()

    outcome = _run(rerank.Reranker(), judge, spec=replace(SPEC, max_usd_per_day=0.01), usage=second)

    assert outcome.reason == "budget_day" and judge.calls == []
    assert second.rerank_usd_today() == pytest.approx(0.01)
    assert _run(rerank.Reranker(), judge, spec=replace(SPEC, max_usd_per_day=0.0), usage=second).applied


def test_a_stale_flush_never_lowers_the_daily_spend_another_process_recorded(tmp_path: Path) -> None:
    """A process whose copy of the spend is behind merges with the file instead of overwriting it."""
    path = tmp_path / "usage_stats.json"
    first, second = UsageTracker(path), UsageTracker(path)
    first.record_rerank(applied=True, reason=None, units=6, input_tokens=6000, usd=0.40)
    second.record_rerank(applied=True, reason=None, units=6, input_tokens=6000, usd=0.50)

    for _ in range(60):
        first.record_rerank(applied=True, reason=None, units=1, cache_hits=1)
    first.flush()

    on_disk = json.loads(path.read_text())["rerank"]["usd_today"]
    assert on_disk == pytest.approx(0.90)
    assert first.rerank_usd_today() == pytest.approx(0.90)
    assert second.rerank_usd_today() == pytest.approx(0.90)
    third = UsageTracker(path)
    assert third.rerank_usd_today() == pytest.approx(0.90)
    judge = FakeJudge()
    outcome = _run(rerank.Reranker(), judge, spec=replace(SPEC, max_usd_per_day=0.90), usage=third)
    assert outcome.reason == "budget_day" and judge.calls == []


def test_every_paid_call_adds_onto_the_spend_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "usage_stats.json"
    first, second = UsageTracker(path), UsageTracker(path)

    first.record_rerank(applied=True, reason=None, usd=0.40)
    second.record_rerank(applied=True, reason=None, usd=0.50)
    first.record_rerank(applied=True, reason=None, usd=0.10)

    assert json.loads(path.read_text())["rerank"]["usd_today"] == pytest.approx(1.00)
    assert not list(tmp_path.glob("*.tmp"))


def test_the_gap_option_reranks_only_close_calls() -> None:
    judge = FakeJudge()

    far = _run(rerank.Reranker(), judge, spec=replace(SPEC, conditional_gap=0.05))
    close = _run(rerank.Reranker(), judge, spec=replace(SPEC, conditional_gap=0.2))

    assert far.reason == "gap" and close.applied
    assert len(judge.calls) == 1


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"allowed": False}, "disabled"),
        ({"judge": NullJudge()}, "unavailable"),
        ({"mode": "semantic"}, "mode"),
        ({"mode": "keyword"}, "mode"),
        ({"include_memories": True}, "memories_included"),
        ({}, None),
        ({"mode": "nonsense"}, None),
    ],
)
def test_the_precheck_names_why_a_search_is_not_reranked(kwargs: dict, reason: str | None) -> None:
    base = {"allowed": True, "judge": FakeJudge(), "mode": "hybrid", "include_memories": False}

    assert rerank.precheck(SPEC, **{**base, **kwargs}) == reason


def test_a_null_judge_with_a_key_is_unavailable_not_key_missing() -> None:
    no_key = replace(SPEC, api_key="")

    assert rerank.precheck(no_key, allowed=True, judge=NullJudge(), mode="hybrid", include_memories=False) == (
        "key_missing"
    )
    assert rerank.precheck(SPEC, allowed=True, judge=NullJudge(), mode="hybrid", include_memories=False) == (
        "unavailable"
    )


def test_requested_follows_the_vault_default_only_when_omitted() -> None:
    on = replace(SPEC, default_on=True)

    assert rerank.requested(None, SPEC) is None
    assert rerank.requested(None, on) == "on"
    assert rerank.requested("off", on) == "off"
    assert rerank.requested(None, RerankSettings(default_on=True)) is None


def test_meta_is_deterministic_and_carries_no_latency() -> None:
    outcome = rerank.Outcome(applied=True, reason=None, candidates=3, units=6, cache_hits=2, latency_ms=12.5)

    assert rerank.meta(SPEC, outcome) == {
        "provider": "jev",
        "model": "jev-1.13.0",
        "applied": True,
        "reason": None,
        "candidates": 3,
        "units": 6,
        "cache_hits": 2,
    }
    assert set(rerank.REASONS) >= {"off", "mode", "key_missing", "budget_day", "breaker_open"}


def test_the_pool_holds_at_least_twenty_documents() -> None:
    assert [rerank.pool_limit(n) for n in (1, 5, 20, 30)] == [20, 20, 20, 30]


# --- usage -----------------------------------------------------------------------


def test_rerank_aggregates_reach_the_usage_file_without_any_text(tmp_path: Path) -> None:
    path = tmp_path / "usage_stats.json"
    tracker = UsageTracker(path)
    tracker.record_search(reranked=True)
    tracker.record_search(reranked=False)
    tracker.record_rerank(applied=True, reason=None, units=6, cache_hits=2, input_tokens=9000, usd=0.000378, latency_ms=240)
    tracker.record_rerank(applied=False, reason="timeout", units=6, latency_ms=1600)
    tracker.flush()

    raw = json.loads(path.read_text())["rerank"]
    assert raw["calls"] == 2 and raw["applied"] == 1 and raw["skipped"] == {"timeout": 1}
    assert raw["latency_ms"] == {"300": 1, "3000": 1}
    summary = rerank_summary(path)
    assert summary["applied_rate"] == 0.5
    assert summary["cache_hit_rate"] == round(2 / 12, 3)
    assert (summary["p50_ms"], summary["p95_ms"]) == (300, 3000)
    assert summary["searches_today"] == 2 and summary["reranked_today"] == 1
    assert math.isclose(summary["usd_today"], 0.000378)


def test_a_usage_file_without_rerank_reads_as_no_activity(tmp_path: Path) -> None:
    summary = rerank_summary(tmp_path / "missing.json")

    assert summary["calls"] == 0 and summary["applied_rate"] is None and summary["p95_ms"] is None
