"""The retrieval evaluation harness (``scripts/eval_retrieval.py``).

Metrics and query-set validation are pinned with hand-computed cases. The
end-to-end case drives a real stdio server over the fixture vault — the same
launcher the golden master uses, so embeddings are the bag-of-words double and
no key or network is involved — and runs the fixture query set through it.
Tokens are counted with a whitespace counter: the real tokenizer downloads its
vocabulary once, and the suite never touches the network.

Nothing here reads a real vault. The query sets a vault keeps in its own
``Eval/`` folder are checked against that vault by the person who edits them,
with the command in ``docs/eval/README.md``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
PROJECT_ROOT = TESTS_DIR.parent
FIXTURE_VAULT = TESTS_DIR / "fixtures" / "vault"
FIXTURE_QUERIES = TESTS_DIR / "fixtures" / "eval" / "queries.yaml"
GOLDEN_SERVER = TESTS_DIR / "golden_stdio_server.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("eval_retrieval", PROJECT_ROOT / "scripts" / "eval_retrieval.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_retrieval"] = module
    spec.loader.exec_module(module)
    return module


ev = _load_script()


def _words(text: str) -> int:
    return len(text.split())


# --- metrics -----------------------------------------------------------------


def test_recall_counts_expected_documents_inside_the_top_k() -> None:
    assert ev.recall_at_k(["a", "b", "c"], ["b", "z"], 5) == 0.5
    assert ev.recall_at_k(["a", "b", "c"], ["c"], 2) == 0.0


def test_precision_divides_by_k_not_by_the_returned_count() -> None:
    assert ev.precision_at_k(["a", "b"], ["a"], 5) == pytest.approx(0.2)
    assert ev.precision_ceiling(["a"], 5) == pytest.approx(0.2)
    assert ev.precision_ceiling(["a", "b", "c", "d", "e", "f"], 5) == 1.0


def test_reciprocal_rank_is_the_first_expected_hit() -> None:
    assert ev.reciprocal_rank(["x", "a", "b"], ["b", "a"], 5) == 0.5
    assert ev.reciprocal_rank(["x", "y"], ["a"], 5) == 0.0
    assert ev.reciprocal_rank(["x", "y", "a"], ["a"], 2) == 0.0


def test_hit_and_primary_at_1_read_only_the_first_row() -> None:
    assert ev.hit_at_1(["B.md", "A.md"], ["A.md", "B.md"]) == 1.0
    assert ev.primary_at_1(["B.md", "A.md"], ["A.md", "B.md"]) == 0.0
    assert ev.primary_at_1(["A.md"], ["A.md", "B.md"]) == 1.0
    assert ev.hit_at_1([], ["A.md"]) == 0.0
    assert ev.primary_at_1([], ["A.md"]) == 0.0


def test_ndcg_is_binary_and_normalized_by_the_best_ordering() -> None:
    import math

    dcg = 1 / math.log2(3) + 1 / math.log2(4)
    ideal = 1 + 1 / math.log2(3) + 1 / math.log2(4)
    assert ev.ndcg_at_k(["X.md", "A.md", "B.md"], ["A.md", "B.md", "C.md"], 5) == pytest.approx(dcg / ideal)
    assert ev.ndcg_at_k(["A.md", "X.md"], ["A.md"], 5) == 1.0
    assert ev.ndcg_at_k(["A.md", "A.md"], ["A.md", "B.md"], 5) == pytest.approx(1 / (1 + 1 / math.log2(3)))
    assert ev.ndcg_at_k(["X.md", "A.md"], ["A.md"], 1) == 0.0
    assert ev.ndcg_at_k([], ["A.md"], 5) == 0.0


def test_percentile_is_nearest_rank() -> None:
    assert ev._percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10
    assert ev._percentile([4], 0.95) == 4
    assert ev._percentile([], 0.95) == 0.0


# --- query sets --------------------------------------------------------------


def test_fixture_query_set_points_at_fixture_documents() -> None:
    qset = ev.load_query_set(FIXTURE_QUERIES)

    assert qset.vault == "fixture"
    assert {q.lang for q in qset.queries} == {"es", "en"}
    missing = [p for q in qset.queries for p in q.expected if not (FIXTURE_VAULT / p).is_file()]
    assert missing == []


def test_fixture_query_set_is_well_formed() -> None:
    qset = ev.load_query_set(FIXTURE_QUERIES)

    assert qset.k == 5
    assert len(qset.queries) == 10
    assert len({q.id for q in qset.queries}) == len(qset.queries)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("version: 2\nqueries: []\n", "version: 1"),
        ("version: 1\nqueries: []\n", "non-empty list"),
        ("version: 1\nk: 0\nqueries: [{id: a, lang: es, query: q, expected: [a.md]}]\n", "positive integer"),
        ("version: 1\nqueries: [{id: a, lang: fr, query: q, expected: [a.md]}]\n", "lang"),
        ("version: 1\nqueries: [{id: a, lang: es, query: ' ', expected: [a.md]}]\n", "query is required"),
        ("version: 1\nqueries: [{id: a, lang: es, query: q, expected: []}]\n", "non-empty list of paths"),
        ("version: 1\nqueries: [{id: a, lang: es, query: q, expected: ['[[A]]']}]\n", "vault-relative"),
        ("version: 1\nqueries: [{id: a, lang: es, query: q, expected: [/abs.md]}]\n", "vault-relative"),
        (
            "version: 1\nqueries: [{id: a, lang: es, query: q, expected: [a.md]}, "
            "{id: a, lang: en, query: r, expected: [b.md]}]\n",
            "duplicate id",
        ),
    ],
    ids=["version", "empty", "k", "lang", "blank-query", "no-expected", "wikilink", "absolute", "duplicate"],
)
def test_malformed_query_sets_are_rejected(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "queries.yaml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ev.EvalError, match=message):
        ev.load_query_set(path)


# --- scoring without a server --------------------------------------------------


def test_run_scores_each_query_from_the_response_blocks() -> None:
    qset = ev.QuerySet(
        vault="t",
        k=2,
        queries=(
            ev.Query("q1", "en", "alpha", ("A.md",)),
            ev.Query("q2", "es", "beta", ("B.md", "C.md")),
        ),
    )
    answers = {
        "alpha": ['{"file_path": "X.md"}', '{"file_path": "A.md"}'],
        "beta": ['{"file_path": "C.md"}'],
    }

    async def fake_search(tool: str, arguments: dict) -> object:
        assert tool == "search" and arguments["limit"] == 2
        return ev.ToolResponse(texts=tuple(answers[arguments["query"]]), is_error=False)

    report = asyncio.run(
        ev.run_query_set(fake_search, qset, _words, label="t", target="fake", encoding="words")
    )
    first, second = report.results

    assert (first.recall, first.reciprocal_rank, first.precision) == (1.0, 0.5, 0.5)
    assert (second.recall, second.reciprocal_rank) == (0.5, 1.0)
    summary = report.summary()
    assert summary["recall_at_k"] == pytest.approx(0.75)
    assert summary["mrr_at_k"] == pytest.approx(0.75)
    assert report.to_json()["by_lang"]["es"]["queries"] == 1


def test_rerank_and_pointers_are_sent_and_every_first_row_is_read_after_the_last_search() -> None:
    qset = ev.QuerySet(
        vault="t",
        k=2,
        queries=(
            ev.Query("q1", "en", "alpha", ("A.md",), answer_terms=("first", "fact")),
            ev.Query("q2", "es", "beta", ("B.md",), answer_terms=("other", "fact")),
            ev.Query("q3", "es", "gamma", ("C.md",)),
        ),
    )
    rows = {
        "alpha": ['{"file_path": "A.md", "chunk_id": "A.md#2"}'],
        "beta": ['{"file_path": "X.md", "chunk_id": "X.md#0"}', '{"file_path": "B.md"}'],
        "gamma": ['{"file_path": "C.md", "chunk_id": "C.md#1"}'],
    }
    metas = {
        "alpha": {"applied": True, "reason": None, "cache_hits": 0},
        "beta": {"applied": False, "reason": "timeout", "cache_hits": 0},
        "gamma": {"applied": True, "reason": None, "cache_hits": 3},
    }
    bodies = {"A.md#2": "the first fact", "X.md#0": "nothing here"}
    calls: list[tuple[str, dict]] = []

    async def fake_call(tool: str, arguments: dict) -> object:
        calls.append((tool, arguments))
        if tool == "search":
            meta = {ev.RERANK_META_KEY: metas[arguments["query"]]}
            return ev.ToolResponse(texts=tuple(rows[arguments["query"]]), is_error=False, meta=meta)
        body = bodies[arguments["chunk_id"]]
        return ev.ToolResponse(texts=(f'{{"content": "{body}"}}',), is_error=False)

    report = asyncio.run(
        ev.run_query_set(
            fake_call, qset, _words, label="t", target="fake", encoding="words", rerank="on", cover=True
        )
    )

    tools = [tool for tool, _ in calls]
    assert tools == ["search", "search", "search", "get_document", "get_document"]
    for _, arguments in calls[:3]:
        assert arguments["rerank"] == "on"
        assert arguments["fields"] == list(ev.POINTER_FIELDS)
    assert calls[3][1] == {"file_path": "A.md", "max_chars": ev.READ_MAX_CHARS, "chunk_id": "A.md#2"}
    first, second, third = report.results
    assert (first.cover_at_1, second.cover_at_1, third.cover_at_1) == (True, False, None)
    assert (first.hit_at_1, second.hit_at_1, second.reciprocal_rank) == (1.0, 0.0, 0.5)
    assert first.chunk_id == "A.md#2" and first.rerank_meta == metas["alpha"]
    summary = report.summary()
    assert summary["cover_at_1"] == 0.5 and summary["covered_queries"] == 2
    assert summary["rerank_evaluated"] == 3 and summary["rerank_applied"] == 2
    assert summary["rerank_skipped"] == {"timeout": 1}
    assert summary["rerank_cache_hits"] == 3
    assert report.to_json()["rerank"] == "on"
    assert "| t | on |" in ev.render_ranking_table([report])


def test_without_rerank_or_cover_the_search_call_is_unchanged() -> None:
    qset = ev.QuerySet(vault="t", k=5, queries=(ev.Query("q1", "en", "alpha", ("A.md",), answer_terms=("a", "b")),))
    seen: list[dict] = []

    async def fake_search(tool: str, arguments: dict) -> object:
        seen.append(arguments)
        return ev.ToolResponse(texts=('{"file_path": "A.md"}',), is_error=False)

    report = asyncio.run(ev.run_query_set(fake_search, qset, _words, label="t", target="f", encoding="w"))

    assert seen == [{"query": "alpha", "limit": 5, "mode": "hybrid"}]
    summary = report.summary()
    assert summary["cover_at_1"] is None and summary["rerank_evaluated"] == 0
    assert report.results[0].rerank_meta is None and report.results[0].chunk_id is None


def test_warmup_searches_run_first_with_the_same_arguments_and_count_in_no_metric() -> None:
    qset = ev.QuerySet(vault="t", k=5, queries=(ev.Query("q1", "en", "alpha", ("A.md",), answer_terms=("a", "b")),))
    seen: list[dict] = []

    async def fake_search(tool: str, arguments: dict) -> object:
        seen.append(arguments)
        return ev.ToolResponse(texts=('{"file_path": "A.md"}',), is_error=False)

    report = asyncio.run(
        ev.run_query_set(fake_search, qset, _words, label="t", target="f", encoding="w", rerank="on", warmup=2)
    )

    warm = {"query": ev.WARMUP_QUERY, "limit": 5, "mode": "hybrid", "rerank": "on"}
    assert seen == [warm, warm, {"query": "alpha", "limit": 5, "mode": "hybrid", "rerank": "on"}]
    assert [r.id for r in report.results] == ["q1"]
    assert report.to_json()["warmup"] == 2


def test_the_rerank_usage_is_read_from_the_index_directory(tmp_path: Path) -> None:
    assert ev.read_rerank_usage(None) is None
    assert ev.read_rerank_usage(tmp_path) is None
    (tmp_path / "usage_stats.json").write_text('{"rerank": {"calls": 2, "usd": 0.001}}', encoding="utf-8")

    assert ev.read_rerank_usage(tmp_path) == {"calls": 2, "usd": 0.001}


def test_pass_env_copies_a_variable_by_name_and_refuses_an_unset_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_TEST_JUDGE_KEY", "value-from-the-shell")
    monkeypatch.delenv("EVAL_TEST_MISSING", raising=False)

    assert ev.pass_env(["EVAL_TEST_JUDGE_KEY"]) == {"EVAL_TEST_JUDGE_KEY": "value-from-the-shell"}
    with pytest.raises(ev.EvalError, match="EVAL_TEST_MISSING"):
        ev.pass_env(["EVAL_TEST_MISSING"])
    args = ev._parse_args(
        ["--stdio", "--vault", "v", "--queries", "q.yaml", "--pass-env", "EVAL_TEST_JUDGE_KEY", "--extra-env", "A=1"]
    )
    assert ev.server_env(args) == {"A": "1", "EVAL_TEST_JUDGE_KEY": "value-from-the-shell"}


def test_an_error_response_scores_zero_and_is_counted() -> None:
    qset = ev.QuerySet(vault="t", k=5, queries=(ev.Query("q1", "en", "alpha", ("A.md",)),))

    async def failing_search(tool: str, arguments: dict) -> object:
        return ev.ToolResponse(texts=("Error executing tool search",), is_error=True)

    report = asyncio.run(ev.run_query_set(failing_search, qset, _words, label="t", target="f", encoding="w"))

    assert report.results[0].recall == 0.0
    assert report.summary()["errors"] == 1


def test_http_mode_without_credentials_fails_with_one_line(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv(ev.URL_ENV, raising=False)
    monkeypatch.delenv(ev.BEARER_ENV, raising=False)
    monkeypatch.setattr(ev, "tiktoken_counter", lambda encoding: _words)

    code = ev.main(["--http", "--queries", str(FIXTURE_QUERIES)])

    assert code == 2
    err = capsys.readouterr().err
    assert ev.URL_ENV in err and "Traceback" not in err


# --- end to end over stdio -------------------------------------------------------


def test_fixture_set_over_a_real_stdio_server(tmp_path: Path) -> None:
    qset = ev.load_query_set(FIXTURE_QUERIES)

    async def run(errlog) -> object:
        async with ev.stdio_session(
            FIXTURE_VAULT,
            index_dir=tmp_path / "chroma",
            server_script=GOLDEN_SERVER,
            extra_env={"OPENAI_API_KEY": "placeholder-not-a-credential"},
            errlog=errlog,
        ) as search:
            return await ev.run_query_set(search, qset, _words, label="fixture", target="stdio", encoding="words")

    with open(tmp_path / "server.log", "w", encoding="utf-8") as errlog:
        report = asyncio.run(run(errlog))
    summary = report.summary()

    assert summary["queries"] == len(qset.queries)
    assert summary["errors"] == 0
    assert summary["recall_at_k"] > 0.5
    assert all(r.returned for r in report.results)
    assert all(r.tokens > 0 for r in report.results)
    markdown = ev.render_markdown(report)
    assert "recall@k" in markdown and "fx-001" in markdown


def test_rerank_over_a_loopback_judge_reports_meta_and_reading_coverage(tmp_path: Path) -> None:
    """``rerank="on"`` against the System One double: every search is judged and its first row read."""
    import shutil

    from systemone_fakes import API_KEY, FakeSystemOne, systemone_server

    vault = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    queries = tmp_path / "queries.yaml"
    queries.write_text(
        "version: 1\nvault: fixture\nk: 3\nqueries:\n"
        "  - id: rr-1\n    lang: en\n    query: middleware outage runbook\n"
        "    expected: ['Runbooks/Runbook - Middleware Outage.md']\n"
        "    answer_terms: ['Steps to triage', 'middleware outage']\n"
        "  - id: rr-2\n    lang: es\n    query: flujo de OTP de Comet Argentina\n"
        "    expected: ['Flows/Flow - Comet Argentina.md']\n",
        encoding="utf-8",
    )
    qset = ev.load_query_set(queries)

    async def run(errlog) -> object:
        async with ev.stdio_session(
            vault,
            index_dir=tmp_path / "index",
            server_script=GOLDEN_SERVER,
            extra_env={"OPENAI_API_KEY": "placeholder-not-a-credential", "KNOWLEDGE_GOLDEN_KEV_API_KEY": API_KEY},
            errlog=errlog,
        ) as call:
            return await ev.run_query_set(
                call, qset, _words, label="rr", target="stdio", encoding="words", rerank="on", cover=True
            )

    with systemone_server(FakeSystemOne()) as (origin, state):
        (vault / "vault.yaml").write_text(
            "rerank:\n  provider: kev\n  model: kev-latest\n  key_env: KEV_API_KEY\n"
            f"  candidates: 5\n  base_url: {origin}\n",
            encoding="utf-8",
        )
        with open(tmp_path / "server.log", "w", encoding="utf-8") as errlog:
            report = asyncio.run(run(errlog))

    summary = report.summary()
    assert summary["errors"] == 0
    assert summary["rerank_evaluated"] == 2 and summary["rerank_applied"] == 2
    assert all(r.rerank_meta and r.rerank_meta["provider"] == "kev" for r in report.results)
    first, second = report.results
    assert first.returned[0] == "Runbooks/Runbook - Middleware Outage.md"
    assert first.chunk_id and first.cover_at_1 is True
    assert second.cover_at_1 is None
    assert summary["cover_at_1"] == 1.0


def test_a_keyless_provider_runs_without_an_openai_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--provider none`` launches the real server with no key; the index lands in ``--index-dir``."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    qset = ev.load_query_set(FIXTURE_QUERIES)
    index_dir = tmp_path / "index"

    async def run(errlog) -> object:
        async with ev.stdio_session(FIXTURE_VAULT, index_dir=index_dir, provider="none", errlog=errlog) as search:
            return await ev.run_query_set(search, qset, _words, label="none", target="stdio", encoding="words")

    with open(tmp_path / "server.log", "w", encoding="utf-8") as errlog:
        report = asyncio.run(run(errlog))

    assert report.summary()["errors"] == 0
    assert report.summary()["recall_at_k"] > 0
    meta = (index_dir / "index_meta.json").read_text(encoding="utf-8")
    assert '"provider": "none"' in meta


def test_the_openai_provider_without_a_key_is_refused_before_launching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def enter() -> None:
        async with ev.stdio_session(FIXTURE_VAULT, index_dir=tmp_path / "index", provider="openai"):
            pass

    with pytest.raises(ev.EvalError, match="OPENAI_API_KEY"):
        asyncio.run(enter())


def test_extra_env_pairs_are_parsed_and_checked() -> None:
    assert ev.parse_env_pairs(["A=1", "B=x=y", "C="]) == {"A": "1", "B": "x=y", "C": ""}
    with pytest.raises(ev.EvalError):
        ev.parse_env_pairs(["missing-equals"])
    with pytest.raises(ev.EvalError):
        ev.parse_env_pairs(["=value"])


def test_the_server_options_parse() -> None:
    args = ev._parse_args(
        ["--stdio", "--vault", "v", "--queries", "q.yaml", "--provider", "none", "--index-dir", "idx", "--extra-env", "K=V"]
    )
    legacy = ev._parse_args(["--stdio", "--vault", "v", "--queries", "q.yaml", "--chroma-dir", "old"])

    assert (args.provider, str(args.index_dir), args.extra_env) == ("none", "idx", ["K=V"])
    assert (legacy.provider, str(legacy.index_dir)) == ("openai", "old")


# --- answer terms ---------------------------------------------------------------


def test_answer_terms_are_optional_and_read_when_present(tmp_path: Path) -> None:
    path = tmp_path / "queries.yaml"
    path.write_text(
        "version: 1\nqueries:\n"
        "  - {id: a, lang: es, query: q, expected: [a.md], answer_terms: [uno, dos]}\n"
        "  - {id: b, lang: en, query: r, expected: [b.md]}\n",
        encoding="utf-8",
    )

    qset = ev.load_query_set(path)

    assert qset.queries[0].answer_terms == ("uno", "dos")
    assert qset.queries[1].answer_terms == ()


@pytest.mark.parametrize("terms", ["[solo]", "[a, b, c, d, e]", "[a, '']", "texto"], ids=["one", "five", "blank", "scalar"])
def test_answer_terms_outside_two_to_four_strings_are_rejected(tmp_path: Path, terms: str) -> None:
    path = tmp_path / "queries.yaml"
    path.write_text(
        f"version: 1\nqueries: [{{id: a, lang: es, query: q, expected: [a.md], answer_terms: {terms}}}]\n",
        encoding="utf-8",
    )

    with pytest.raises(ev.EvalError, match="answer_terms"):
        ev.load_query_set(path)


def test_label_check_flags_a_missing_file_and_a_term_absent_from_the_primary_document(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("The port is 3100 by default.\n", encoding="utf-8")
    qset = ev.QuerySet(
        vault="t",
        k=5,
        queries=(
            ev.Query("ok", "en", "q", ("a.md",), answer_terms=("3100", "default")),
            ev.Query("term", "en", "q", ("a.md",), answer_terms=("3100", "8080")),
            ev.Query("file", "en", "q", ("a.md", "gone.md"), answer_terms=("3100", "default")),
        ),
    )

    problems = ev.label_problems(qset, tmp_path)

    assert problems == [
        "term: answer term not in a.md: '8080'",
        "file: expected file not found: gone.md",
    ]


def test_covers_answer_needs_every_term_and_at_least_one() -> None:
    labeled = ev.Query("q", "en", "q", ("a.md",), answer_terms=("alpha", "beta"))

    assert ev.covers_answer("alpha and beta", labeled)
    assert not ev.covers_answer("alpha only", labeled)
    assert not ev.covers_answer("anything", ev.Query("q", "en", "q", ("a.md",)))


def test_check_labels_mode_starts_no_server_and_reports_by_exit_code(tmp_path: Path, capsys) -> None:
    (tmp_path / "a.md").write_text("alpha beta\n", encoding="utf-8")
    good = tmp_path / "good.yaml"
    good.write_text(
        "version: 1\nqueries: [{id: a, lang: es, query: q, expected: [a.md], answer_terms: [alpha, beta]}]\n",
        encoding="utf-8",
    )
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "version: 1\nqueries: [{id: a, lang: es, query: q, expected: [a.md], answer_terms: [alpha, gamma]}]\n",
        encoding="utf-8",
    )

    assert ev.main(["--check-labels", "--vault", str(tmp_path), "--queries", str(good)]) == 0
    assert ev.main(["--check-labels", "--vault", str(tmp_path), "--queries", str(bad)]) == 1
    assert "gamma" in capsys.readouterr().out
