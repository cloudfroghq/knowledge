#!/usr/bin/env python3
"""Run a labeled query set against a knowledge MCP server and score the retrieval.

The server is reached the way every client reaches it, through the tool
contract: one ``search`` call per query over a real MCP session. Two targets:

* ``--stdio``: launch a local server process over stdio. ``--vault`` sets its
  ``VAULT_PATH`` and the index is built in a throwaway directory unless
  ``--index-dir`` names one. ``--provider`` picks the embeddings provider
  (``openai`` by default, which needs ``OPENAI_API_KEY`` in the environment;
  ``none`` and ``bag-of-words`` need no key), ``--extra-env KEY=VALUE`` adds a
  variable to the server's environment, and ``--server-script`` swaps the
  default ``python -m knowledge.server`` for another launcher (the suite passes
  ``tests/golden_stdio_server.py``, which embeds with the bag-of-words double).
* ``--http``: a deployed server. The URL comes from ``KNOWLEDGE_EVAL_URL`` and
  the bearer from ``KNOWLEDGE_EVAL_BEARER``, both from the environment only, so
  neither ever appears in a process listing, a report or a log line.

Metrics, per query and averaged over the set, with ``k`` from the query set:

* recall@k: share of the expected documents that appear in the top k.
* precision@k: expected documents in the top k divided by k. With one to three
  expected documents per query its ceiling is well under 1, so the report
  prints the ceiling next to it.
* MRR@k: reciprocal rank of the first expected document in the top k, 0 when
  none appears.
* hit@1 and prim@1: whether the first row is an expected document, and
  whether it is the primary one (``expected[0]``).
* nDCG@k: binary relevance, discounted by ``log2(rank + 1)`` and normalized
  by the best ordering of ``min(len(expected), k)`` documents.
* cover@1 (with ``--cover``): the read protocol of the server's
  ``instructions``. Every ``search`` carries the pointer fields
  (``file_path``, ``chunk_id``, ``section``, ``score``) and, once every query
  has been searched, the first row of each is read with
  ``get_document(file_path, chunk_id=..., max_chars=2000)``; the query is
  covered when that read holds every ``answer_terms`` literal. The reads run
  after the last search, so the usage they record never moves a ranking that
  is being measured.
* tokens per query: the text content blocks of the ``search`` response — what
  enters a model's context — counted with a real tokenizer (tiktoken,
  ``cl100k_base`` by default). It is a proxy: Claude's tokenizer is not public.

``--rerank on|off`` passes ``rerank`` to every ``search`` (omitted, the
parameter is not sent and the vault's ``rerank.default`` applies). The report
keeps, per query, the ``_meta["knowledge/rerank"]`` the server attached, and
counts how many searches the judge reordered and why the rest were skipped;
with ``--stdio`` and ``--index-dir`` it also copies the ``rerank`` aggregates
of the vault's ``usage_stats.json`` (billed tokens, cost, judge latency
buckets), which is where the cost per query comes from.

A search records usage on the server it runs against (the bounded ranking
boost and the co-retrieval pairs), so running the set against a live
deployment nudges that deployment's ranking. The report says so.

``--check-labels`` reads the query set and the vault and starts no server: it
fails on an expected file that does not exist and on an answer term that is
not in the primary expected document.

Usage::

    OPENAI_API_KEY=... uv run python scripts/eval_retrieval.py --stdio \\
        --vault "$VAULT" --queries "$VAULT/Eval/queries.yaml"
    uv run python scripts/eval_retrieval.py --stdio --provider none \\
        --vault "$VAULT" --queries "$VAULT/Eval/queries.yaml"
    OPENAI_API_KEY=... TYPESAFE_API_KEY=... uv run python scripts/eval_retrieval.py \\
        --stdio --vault "$VAULT" --queries "$VAULT/Eval/queries.yaml" \\
        --index-dir build/eval/idx-rr-on --rerank on --cover --json build/eval/rr-on.json
    KNOWLEDGE_EVAL_URL=... KNOWLEDGE_EVAL_BEARER=... uv run python \\
        scripts/eval_retrieval.py --http --queries "$VAULT/Eval/queries.yaml" \\
        --json build/eval/report.json --markdown build/eval/report.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, TextIO

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
#: Variable through which the local server takes its index directory; it
#: wins over ``CHROMA_PERSIST_DIR``, which the session pins empty.
INDEX_DIR_ENV = "KNOWLEDGE_INDEX_DIR"

#: Embeddings provider of a ``--stdio`` server unless ``--provider`` says otherwise.
DEFAULT_PROVIDER = "openai"

#: Key of ``_meta`` under which the server reports what the judge did for one ``search``.
RERANK_META_KEY = "knowledge/rerank"
#: Values of ``--rerank``: the ``rerank`` argument of ``search``.
RERANK_CHOICES = ("on", "off")
#: ``fields`` of every ``search`` under ``--cover``: the pointers the ``instructions`` protocol names.
POINTER_FIELDS = ("file_path", "chunk_id", "section", "score")
#: ``max_chars`` of the ``get_document`` read that ``--cover`` scores.
READ_MAX_CHARS = 2000

URL_ENV = "KNOWLEDGE_EVAL_URL"
BEARER_ENV = "KNOWLEDGE_EVAL_BEARER"
DEFAULT_ENCODING = "cl100k_base"

TokenCounter = Callable[[str], int]
SearchFn = Callable[[str, dict[str, Any]], Awaitable["ToolResponse"]]


class EvalError(Exception):
    """A failure the command reports as one line, never with a traceback."""


# --- query set --------------------------------------------------------------


@dataclass(frozen=True)
class Query:
    id: str
    lang: str
    query: str
    expected: tuple[str, ...]
    note: str = ""
    #: Literals that sit in the passage of ``expected[0]`` that answers the
    #: query; empty when the query is not labeled with any.
    answer_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuerySet:
    vault: str
    k: int
    queries: tuple[Query, ...]


def load_query_set(path: Path) -> QuerySet:
    """Parse and validate a query set; every defect is an :class:`EvalError`."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise EvalError(f"cannot read query set {path}: {exc}") from None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise EvalError(f"{path}: expected a mapping with version: 1")
    k = raw.get("k", 5)
    if not isinstance(k, int) or k < 1:
        raise EvalError(f"{path}: k must be a positive integer")
    items = raw.get("queries")
    if not isinstance(items, list) or not items:
        raise EvalError(f"{path}: queries must be a non-empty list")

    queries: list[Query] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        where = f"{path}: queries[{i}]"
        if not isinstance(item, dict):
            raise EvalError(f"{where} is not a mapping")
        qid, lang, text, expected = item.get("id"), item.get("lang"), item.get("query"), item.get("expected")
        if not isinstance(qid, str) or not qid:
            raise EvalError(f"{where}: id is required")
        if qid in seen:
            raise EvalError(f"{where}: duplicate id {qid!r}")
        seen.add(qid)
        if lang not in ("es", "en"):
            raise EvalError(f"{where} ({qid}): lang must be 'es' or 'en'")
        if not isinstance(text, str) or not text.strip():
            raise EvalError(f"{where} ({qid}): query is required")
        if not isinstance(expected, list) or not expected or not all(isinstance(p, str) and p for p in expected):
            raise EvalError(f"{where} ({qid}): expected must be a non-empty list of paths")
        for p in expected:
            if p.startswith("/") or p.startswith("[[") or not p.endswith(".md"):
                raise EvalError(f"{where} ({qid}): {p!r} must be a vault-relative .md path")
        terms = item.get("answer_terms", [])
        if not isinstance(terms, list) or (
            terms and not (2 <= len(terms) <= 4 and all(isinstance(t, str) and t for t in terms))
        ):
            raise EvalError(f"{where} ({qid}): answer_terms must be a list of 2 to 4 non-empty strings")
        queries.append(Query(qid, lang, text, tuple(expected), str(item.get("note", "")), tuple(terms)))
    return QuerySet(vault=str(raw.get("vault", "")), k=k, queries=tuple(queries))


def label_problems(qset: QuerySet, vault: Path) -> list[str]:
    """Labels that do not hold against ``vault``: a missing file or a term not in ``expected[0]``.

    This reads the vault directly and starts no server. It is the check the
    person who edits a query set runs against the real vault; the suite only
    runs it on a synthetic one.
    """
    problems: list[str] = []
    for q in qset.queries:
        for rel in q.expected:
            if not (vault / rel).is_file():
                problems.append(f"{q.id}: expected file not found: {rel}")
        primary = vault / q.expected[0]
        if not primary.is_file():
            continue
        body = primary.read_text(encoding="utf-8")
        for term in q.answer_terms:
            if term not in body:
                problems.append(f"{q.id}: answer term not in {q.expected[0]}: {term!r}")
    return problems


def covers_answer(text: str, query: Query) -> bool:
    """Whether a read contains every answer term of ``query``; false when it has none."""
    return bool(query.answer_terms) and all(term in text for term in query.answer_terms)


# --- metrics ----------------------------------------------------------------


def recall_at_k(returned: Sequence[str], expected: Sequence[str], k: int) -> float:
    relevant = set(expected)
    return len(relevant & set(returned[:k])) / len(relevant)


def precision_at_k(returned: Sequence[str], expected: Sequence[str], k: int) -> float:
    return len(set(expected) & set(returned[:k])) / k


def reciprocal_rank(returned: Sequence[str], expected: Sequence[str], k: int) -> float:
    relevant = set(expected)
    for rank, path in enumerate(returned[:k], start=1):
        if path in relevant:
            return 1.0 / rank
    return 0.0


def precision_ceiling(expected: Sequence[str], k: int) -> float:
    """The best precision@k a perfect ranking could reach for this query."""
    return min(len(set(expected)), k) / k


def hit_at_1(returned: Sequence[str], expected: Sequence[str]) -> float:
    """1 when the first row is any expected document."""
    return 1.0 if returned and returned[0] in set(expected) else 0.0


def primary_at_1(returned: Sequence[str], expected: Sequence[str]) -> float:
    """1 when the first row is the primary expected document, ``expected[0]``."""
    return 1.0 if returned and expected and returned[0] == expected[0] else 0.0


def ndcg_at_k(returned: Sequence[str], expected: Sequence[str], k: int) -> float:
    """Binary-relevance nDCG@k: each expected document in the top k counts once."""
    relevant = set(expected)
    seen: set[str] = set()
    dcg = 0.0
    for rank, path in enumerate(returned[:k], start=1):
        if path in relevant and path not in seen:
            seen.add(path)
            dcg += 1.0 / math.log2(rank + 1)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


# --- tokens -----------------------------------------------------------------


def tiktoken_counter(encoding: str = DEFAULT_ENCODING) -> TokenCounter:
    """A token counter over a tiktoken encoding (downloaded once, then cached)."""
    import tiktoken

    enc = tiktoken.get_encoding(encoding)
    return lambda text: len(enc.encode(text, disallowed_special=()))


# --- talking to the server ---------------------------------------------------


@dataclass(frozen=True)
class ToolResponse:
    """The part of a ``tools/call`` result the evaluation reads."""

    texts: tuple[str, ...]
    is_error: bool
    #: ``_meta`` of the result, when the server attached one.
    meta: dict[str, Any] | None = None

    def items(self) -> list[Any]:
        """Each text block parsed as JSON (FastMCP renders one block per list item)."""
        out: list[Any] = []
        for text in self.texts:
            try:
                out.append(json.loads(text))
            except json.JSONDecodeError:
                out.append(text)
        return out


def _to_response(result: Any) -> ToolResponse:
    texts = tuple(block.text for block in result.content if getattr(block, "type", None) == "text")
    meta = getattr(result, "meta", None)
    return ToolResponse(texts=texts, is_error=bool(result.isError), meta=dict(meta) if meta else None)


@asynccontextmanager
async def stdio_session(
    vault: Path,
    *,
    index_dir: Path | None = None,
    provider: str = DEFAULT_PROVIDER,
    server_script: Path | None = None,
    extra_env: dict[str, str] | None = None,
    errlog: TextIO | None = None,
):
    """A session with a fresh local server over stdio, yielding a search function.

    ``provider`` is pinned in the server's environment, so a ``.env`` cannot
    swap it; only ``openai`` needs ``OPENAI_API_KEY``. ``extra_env`` is applied
    last. The server logs every request at INFO on stderr; ``errlog`` receives
    that stream (a real file: the SDK hands its descriptor to the subprocess).
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    if not vault.is_dir():
        raise EvalError(f"--vault is not a directory: {vault}")
    with tempfile.TemporaryDirectory(prefix="knowledge-eval-") as tmp:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "VAULT_PATH": str(vault.resolve()),
            INDEX_DIR_ENV: str((index_dir or Path(tmp) / "index").resolve()),
            "CHROMA_PERSIST_DIR": "",
            "KNOWLEDGE_EMBEDDINGS_PROVIDER": provider,
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
            "MCP_TRANSPORT": "stdio",
            "CONFLUENCE_URL": "",
            "CONFLUENCE_EMAIL": "",
            "CONFLUENCE_TOKEN": "",
            # Pinned empty so a ``.env`` cannot point the evaluation at a
            # memory server: the measured server reads the vault alone.
            "MEMORY_HTTP_TOKEN": "",
            "KNOWLEDGE_MEMORY_URL": "",
            # The default output, whatever a ``.env`` says: the token counts
            # are of the compact text blocks a client reads.
            "KNOWLEDGE_STRUCTURED_OUTPUT": "",
            "KNOWLEDGE_TEXT_INDENT": "",
            "PYTHONHASHSEED": "0",
            **(extra_env or {}),
        }
        if env["KNOWLEDGE_EMBEDDINGS_PROVIDER"] == "openai" and not env["OPENAI_API_KEY"]:
            raise EvalError("OPENAI_API_KEY is not set; the openai provider needs it to embed the vault")
        args = [str(server_script)] if server_script else ["-m", "knowledge.server"]
        params = StdioServerParameters(command=sys.executable, args=args, env=env, cwd=str(PROJECT_ROOT))
        async with stdio_client(params, errlog=errlog or sys.stderr) as (read, write):
            # The first search indexes the whole vault; generous on purpose.
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=1800)) as session:
                await session.initialize()

                async def call(tool: str, arguments: dict[str, Any]) -> ToolResponse:
                    return _to_response(await session.call_tool(tool, arguments=arguments))

                yield call


@asynccontextmanager
async def http_session():
    """A session with a deployed server; URL and bearer from the environment only."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url, bearer = os.environ.get(URL_ENV, ""), os.environ.get(BEARER_ENV, "")
    if not url or not bearer:
        raise EvalError(f"--http needs {URL_ENV} and {BEARER_ENV} in the environment")
    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {bearer}"}) as (read, write, _):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=300)) as session:
            await session.initialize()

            async def call(tool: str, arguments: dict[str, Any]) -> ToolResponse:
                return _to_response(await session.call_tool(tool, arguments=arguments))

            yield call


# --- the run ------------------------------------------------------------------


@dataclass
class QueryResult:
    id: str
    lang: str
    query: str
    expected: list[str]
    returned: list[str]
    recall: float
    precision: float
    reciprocal_rank: float
    precision_ceiling: float
    tokens: int
    chars: int
    latency_ms: float
    error: bool = False
    hit_at_1: float = 0.0
    primary_at_1: float = 0.0
    ndcg: float = 0.0
    #: Whether the read of the first row holds every answer term; ``None`` when not measured or unlabeled.
    cover_at_1: bool | None = None
    #: ``chunk_id`` of the first row, under ``--cover``.
    chunk_id: str | None = None
    #: ``_meta["knowledge/rerank"]`` of the response, when the server evaluated the judge.
    rerank_meta: dict[str, Any] | None = None


@dataclass
class Report:
    label: str
    target: str
    vault: str
    k: int
    encoding: str
    mode: str
    results: list[QueryResult] = field(default_factory=list)
    #: ``rerank`` argument sent with every ``search``; ``None`` when it was not sent.
    rerank: str | None = None
    #: The ``rerank`` section of the vault's ``usage_stats.json`` after the run, when it could be read.
    rerank_usage: dict[str, Any] | None = None
    #: Searches made before the first clocked query and left out of every metric.
    warmup: int = 0

    def summary(self, subset: Sequence[QueryResult] | None = None) -> dict[str, float]:
        rows = list(self.results if subset is None else subset)
        if not rows:
            return {}
        tokens = sorted(r.tokens for r in rows)
        latencies = sorted(r.latency_ms for r in rows)
        covered = [r.cover_at_1 for r in rows if r.cover_at_1 is not None]
        judged = [r.rerank_meta for r in rows if r.rerank_meta is not None]
        skipped: dict[str, int] = {}
        for meta in judged:
            if not meta.get("applied"):
                reason = str(meta.get("reason"))
                skipped[reason] = skipped.get(reason, 0) + 1
        return {
            "queries": len(rows),
            "recall_at_k": statistics.fmean(r.recall for r in rows),
            "precision_at_k": statistics.fmean(r.precision for r in rows),
            "precision_ceiling": statistics.fmean(r.precision_ceiling for r in rows),
            "mrr_at_k": statistics.fmean(r.reciprocal_rank for r in rows),
            "hit_rate_at_k": statistics.fmean(1.0 if r.reciprocal_rank > 0 else 0.0 for r in rows),
            "hit_at_1": statistics.fmean(r.hit_at_1 for r in rows),
            "primary_at_1": statistics.fmean(r.primary_at_1 for r in rows),
            "ndcg_at_k": statistics.fmean(r.ndcg for r in rows),
            "cover_at_1": statistics.fmean(1.0 if c else 0.0 for c in covered) if covered else None,
            "covered_queries": len(covered),
            "tokens_mean": statistics.fmean(tokens),
            "tokens_median": statistics.median(tokens),
            "tokens_p95": _percentile(tokens, 0.95),
            "latency_ms_median": statistics.median(latencies),
            "latency_ms_p95": _percentile(latencies, 0.95),
            "errors": sum(1 for r in rows if r.error),
            "rerank_evaluated": len(judged),
            "rerank_applied": sum(1 for meta in judged if meta.get("applied")),
            "rerank_skipped": dict(sorted(skipped.items())),
            "rerank_cache_hits": sum(int(meta.get("cache_hits") or 0) for meta in judged),
        }

    def to_json(self) -> dict[str, Any]:
        by_lang = {
            lang: self.summary([r for r in self.results if r.lang == lang])
            for lang in sorted({r.lang for r in self.results})
        }
        return {
            "label": self.label,
            "target": self.target,
            "vault": self.vault,
            "k": self.k,
            "encoding": self.encoding,
            "mode": self.mode,
            "rerank": self.rerank,
            "warmup": self.warmup,
            "summary": self.summary(),
            "by_lang": by_lang,
            "rerank_usage": self.rerank_usage,
            "results": [asdict(r) for r in self.results],
        }


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile over an already sorted sequence."""
    if not sorted_values:
        return 0.0
    index = max(0, min(len(sorted_values) - 1, round(q * len(sorted_values) + 0.5) - 1))
    return float(sorted_values[index])


def returned_paths(response: ToolResponse) -> list[str]:
    return [item["file_path"] for item in response.items() if isinstance(item, dict) and item.get("file_path")]


#: The query of a warm-up search: outside every query set, so it warms no cache a clocked query reads.
WARMUP_QUERY = "warm-up search before the clocked queries"


async def run_query_set(
    search: SearchFn,
    qset: QuerySet,
    counter: TokenCounter,
    *,
    label: str,
    target: str,
    encoding: str,
    mode: str = "hybrid",
    rerank: str | None = None,
    cover: bool = False,
    warmup: int = 0,
) -> Report:
    """One ``search`` per query, in file order, scored against its labels.

    ``rerank`` is sent as the ``rerank`` argument when given. With ``cover``
    every search asks for :data:`POINTER_FIELDS` and, after the last search,
    the first row of each labeled query is read and scored (see
    :func:`read_first_rows`). ``warmup`` searches for :data:`WARMUP_QUERY`,
    with the same arguments, run first and count in no metric: they open the
    connections the first clocked query would otherwise pay for.
    """
    report = Report(
        label=label,
        target=target,
        vault=qset.vault,
        k=qset.k,
        encoding=encoding,
        mode=mode,
        rerank=rerank,
        warmup=max(0, warmup),
    )

    def search_arguments(query: str) -> dict[str, Any]:
        arguments: dict[str, Any] = {"query": query, "limit": qset.k, "mode": mode}
        if cover:
            arguments["fields"] = list(POINTER_FIELDS)
        if rerank is not None:
            arguments["rerank"] = rerank
        return arguments

    for _ in range(report.warmup):
        await search("search", search_arguments(WARMUP_QUERY))
    pointers: dict[str, dict[str, Any]] = {}
    for q in qset.queries:
        arguments = search_arguments(q.query)
        started = time.perf_counter()
        response = await search("search", arguments)
        latency_ms = (time.perf_counter() - started) * 1000
        returned = [] if response.is_error else returned_paths(response)
        text = "".join(response.texts)
        first = next((item for item in response.items() if isinstance(item, dict)), None)
        if cover and first and not response.is_error:
            pointers[q.id] = first
        meta = (response.meta or {}).get(RERANK_META_KEY)
        report.results.append(
            QueryResult(
                id=q.id,
                lang=q.lang,
                query=q.query,
                expected=list(q.expected),
                returned=returned,
                recall=recall_at_k(returned, q.expected, qset.k),
                precision=precision_at_k(returned, q.expected, qset.k),
                reciprocal_rank=reciprocal_rank(returned, q.expected, qset.k),
                precision_ceiling=precision_ceiling(q.expected, qset.k),
                tokens=counter(text),
                chars=len(text),
                latency_ms=round(latency_ms, 1),
                error=response.is_error,
                hit_at_1=hit_at_1(returned, q.expected),
                primary_at_1=primary_at_1(returned, q.expected),
                ndcg=ndcg_at_k(returned, q.expected, qset.k),
                chunk_id=(first or {}).get("chunk_id") if cover else None,
                rerank_meta=dict(meta) if isinstance(meta, dict) else None,
            )
        )
    if cover:
        await read_first_rows(search, qset, report, pointers)
    return report


async def read_first_rows(
    call: SearchFn, qset: QuerySet, report: Report, pointers: dict[str, dict[str, Any]]
) -> None:
    """Score cover@1: read the first row of each labeled query the way the ``instructions`` protocol does.

    ``get_document(file_path, chunk_id=..., max_chars=READ_MAX_CHARS)``, or
    from the top of the file when the row names no ``chunk_id``. A query
    without answer terms, or whose search returned no row, stays ``None``.
    """
    by_id = {q.id: q for q in qset.queries}
    for result in report.results:
        query, row = by_id[result.id], pointers.get(result.id)
        if not query.answer_terms or not row or not row.get("file_path"):
            continue
        arguments: dict[str, Any] = {"file_path": row["file_path"], "max_chars": READ_MAX_CHARS}
        if row.get("chunk_id"):
            arguments["chunk_id"] = row["chunk_id"]
        read = await call("get_document", arguments)
        items = read.items()
        document = items[0] if items and isinstance(items[0], dict) else {}
        body = document.get("content") if isinstance(document.get("content"), str) else "".join(read.texts)
        result.cover_at_1 = False if read.is_error else covers_answer(body, query)


def read_rerank_usage(index_dir: Path | None) -> dict[str, Any] | None:
    """The ``rerank`` section of ``usage_stats.json`` in ``index_dir``, or ``None`` when absent."""
    if index_dir is None:
        return None
    try:
        raw = json.loads((index_dir / "usage_stats.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    section = raw.get("rerank") if isinstance(raw, dict) else None
    return section if isinstance(section, dict) else None


# --- rendering ----------------------------------------------------------------


def _fmt(value: float) -> str:
    return f"{value:.3f}"


def render_summary_table(reports: Sequence[Report]) -> str:
    """One row per report: the headline numbers, side by side."""
    lines = [
        "| Corrida | Consultas | recall@k | precisión@k (techo) | MRR@k | acierto@k "
        "| tokens/consulta (media · mediana · p95) | latencia ms (mediana · p95) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for rep in reports:
        s = rep.summary()
        lines.append(
            f"| {rep.label} | {s['queries']} | {_fmt(s['recall_at_k'])} | {_fmt(s['precision_at_k'])} "
            f"({_fmt(s['precision_ceiling'])}) | {_fmt(s['mrr_at_k'])} | {_fmt(s['hit_rate_at_k'])} | "
            f"{s['tokens_mean']:.0f} · {s['tokens_median']:.0f} · {s['tokens_p95']:.0f} | "
            f"{s['latency_ms_median']:.0f} · {s['latency_ms_p95']:.0f} |"
        )
    return "\n".join(lines)


def render_ranking_table(reports: Sequence[Report]) -> str:
    """One row per report: where the first expected document lands and what the first read covers."""
    lines = [
        "| Corrida | rerank | hit@1 | prim@1 | nDCG@k | cover@1 | rerank aplicado |",
        "|---|---|---|---|---|---|---|",
    ]
    for rep in reports:
        s = rep.summary()
        cover = "—" if s["cover_at_1"] is None else f"{_fmt(s['cover_at_1'])} ({s['covered_queries']})"
        applied = f"{s['rerank_applied']} de {s['rerank_evaluated']}" if s["rerank_evaluated"] else "—"
        lines.append(
            f"| {rep.label} | {rep.rerank or 'omitido'} | {_fmt(s['hit_at_1'])} | {_fmt(s['primary_at_1'])} | "
            f"{_fmt(s['ndcg_at_k'])} | {cover} | {applied} |"
        )
    return "\n".join(lines)


def render_per_query_table(report: Report) -> str:
    lines = [
        "| Id | Idioma | recall | RR | tokens | Primer esperado en top-k |",
        "|---|---|---|---|---|---|",
    ]
    for r in report.results:
        rank = next((i for i, p in enumerate(r.returned[: report.k], start=1) if p in set(r.expected)), None)
        lines.append(
            f"| {r.id} | {r.lang} | {r.recall:.2f} | {r.reciprocal_rank:.2f} | {r.tokens} | "
            f"{'—' if rank is None else rank} |"
        )
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    by_lang = report.to_json()["by_lang"]
    lang_rows = [
        "| Idioma | Consultas | recall@k | precisión@k | MRR@k | hit@1 | nDCG@k | tokens/consulta (media) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for lang, s in by_lang.items():
        lang_rows.append(
            f"| {lang} | {s['queries']} | {_fmt(s['recall_at_k'])} | {_fmt(s['precision_at_k'])} | "
            f"{_fmt(s['mrr_at_k'])} | {_fmt(s['hit_at_1'])} | {_fmt(s['ndcg_at_k'])} | {s['tokens_mean']:.0f} |"
        )
    return "\n\n".join(
        [
            f"### {report.label}",
            f"Destino: {report.target}. Vault: `{report.vault}`. k = {report.k}. Modo de `search`: "
            f"`{report.mode}`. Tokenizer: tiktoken `{report.encoding}`.",
            render_summary_table([report]),
            render_ranking_table([report]),
            "\n".join(lang_rows),
            render_per_query_table(report),
        ]
    )


# --- command line -------------------------------------------------------------


def parse_env_pairs(pairs: Sequence[str]) -> dict[str, str]:
    """``KEY=VALUE`` strings as a mapping; a pair without ``=`` or with an empty key is an error."""
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise EvalError(f"--extra-env expects KEY=VALUE, got {pair!r}")
        out[key.strip()] = value
    return out


def pass_env(names: Sequence[str]) -> dict[str, str]:
    """The current value of each variable in ``names``, to hand to a local server by name only.

    A secret such as a judge key reaches the server this way and never sits in
    a command line, where ``--extra-env`` would put it. A name that is unset or
    empty is an error: the run would otherwise measure a server without it.
    """
    out: dict[str, str] = {}
    for name in names:
        value = os.environ.get(name, "")
        if not value:
            raise EvalError(f"--pass-env {name}: the variable is not set in this environment")
        out[name] = value
    return out


def server_env(args: argparse.Namespace) -> dict[str, str]:
    """``--extra-env`` pairs, then ``--pass-env`` variables: the extra environment of a local server."""
    return {**parse_env_pairs(args.extra_env), **pass_env(args.pass_env)}


def add_server_arguments(parser: argparse.ArgumentParser, owner: str) -> None:
    """``--index-dir``, ``--provider``, ``--extra-env`` and ``--pass-env`` for a local server launched by ``owner``."""
    parser.add_argument(
        "--index-dir",
        "--chroma-dir",
        dest="index_dir",
        type=Path,
        help=f"index directory for {owner} (default: throwaway)",
    )
    parser.add_argument(
        "--provider",
        default=DEFAULT_PROVIDER,
        choices=("openai", "none", "bag-of-words"),
        help=f"embeddings provider of the {owner} server; only openai needs OPENAI_API_KEY",
    )
    parser.add_argument(
        "--extra-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=f"extra variable for the {owner} server's environment (repeatable)",
    )
    parser.add_argument(
        "--pass-env",
        action="append",
        default=[],
        metavar="NAME",
        help=f"copy this variable of the current environment into the {owner} server's, by name (repeatable)",
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--stdio", action="store_true", help="launch a local server over stdio")
    target.add_argument("--http", action="store_true", help=f"use a deployed server ({URL_ENV}, {BEARER_ENV})")
    parser.add_argument("--queries", type=Path, required=True, help="query set YAML")
    parser.add_argument("--vault", type=Path, help="VAULT_PATH for --stdio")
    add_server_arguments(parser, "--stdio")
    parser.add_argument("--server-script", type=Path, help="launcher script for --stdio instead of knowledge.server")
    parser.add_argument("--server-log", type=Path, help="file for the --stdio server's stderr (default: discarded)")
    parser.add_argument("--mode", default="hybrid", choices=("hybrid", "semantic", "keyword"))
    parser.add_argument(
        "--rerank",
        choices=RERANK_CHOICES,
        help="send rerank=on|off with every search (default: not sent, the vault's rerank.default applies)",
    )
    parser.add_argument(
        "--cover",
        action="store_true",
        help="search with the pointer fields and score cover@1 by reading each first row with get_document",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=0,
        metavar="N",
        help="searches for a fixed query outside the set before the clocked ones, left out of every metric",
    )
    parser.add_argument("--encoding", default=DEFAULT_ENCODING, help="tiktoken encoding")
    parser.add_argument("--label", help="name of the run in the report")
    parser.add_argument(
        "--check-labels",
        action="store_true",
        help="check the query set against --vault (files exist, answer terms appear) and exit; starts no server",
    )
    parser.add_argument("--json", type=Path, help="write the full report as JSON")
    parser.add_argument("--markdown", type=Path, help="write the report section as Markdown")
    args = parser.parse_args(argv)
    if args.check_labels:
        if args.vault is None:
            parser.error("--check-labels needs --vault")
    elif not (args.stdio or args.http):
        parser.error("one of --stdio, --http or --check-labels is required")
    if args.stdio and args.vault is None:
        parser.error("--stdio needs --vault")
    return args


async def _main_async(args: argparse.Namespace) -> Report:
    qset = load_query_set(args.queries)
    counter = tiktoken_counter(args.encoding)
    label = args.label or f"{qset.vault} ({'stdio' if args.stdio else 'http'})"
    if args.http:
        async with http_session() as search:
            return await run_query_set(
                search, qset, counter, label=label, target="despliegue remoto por HTTP",
                encoding=args.encoding, mode=args.mode, rerank=args.rerank, cover=args.cover,
                warmup=args.warmup,
            )
    with open(args.server_log or os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_session(
            args.vault,
            index_dir=args.index_dir,
            provider=args.provider,
            server_script=args.server_script,
            extra_env=server_env(args),
            errlog=errlog,
        ) as search:
            report = await run_query_set(
                search, qset, counter, label=label, target="servidor local por stdio",
                encoding=args.encoding, mode=args.mode, rerank=args.rerank, cover=args.cover,
                warmup=args.warmup,
            )
    report.rerank_usage = read_rerank_usage(args.index_dir)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.check_labels:
        try:
            problems = label_problems(load_query_set(args.queries), args.vault)
        except EvalError as exc:
            print(f"eval: {exc}", file=sys.stderr)
            return 2
        for problem in problems:
            print(problem)
        print(f"{len(problems)} label problem(s)")
        return 1 if problems else 0
    try:
        report = asyncio.run(_main_async(args))
    except EvalError as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - one line, never a traceback that could echo a header
        print(f"eval: {type(exc).__name__} while talking to the server", file=sys.stderr)
        return 1

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report.to_json(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(report) + "\n", encoding="utf-8")
    print(render_summary_table([report]))
    print(render_ranking_table([report]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
