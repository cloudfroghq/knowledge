#!/usr/bin/env python3
"""Count the tokens every tool response and every typical flow costs a client.

The corpus is the golden master of the declared contract version
(``tests/golden/v<version>/``): the exact ``tools/list``
frame and one ``tools/call`` result per tool, recorded over the stdio
transport against the fixture vault. Using it means the numbers are pinned to
the same bytes the contract test guards, and they move only when the contract
does. The fixture documents are small, so the per-tool numbers are a floor;
the real-vault cost of ``search`` is measured by ``scripts/eval_retrieval.py``.

Two quantities per response:

* content: the text blocks of the result, what a model reads.
* wire: the whole result as compact JSON, ``structuredContent`` and
  ``isError`` included, what crosses the transport.

Flows add the responses a client typically chains:

* ``search -> get_document``: find a document, then read it.
* ``search -> get_document(chunk_id)``: the default rows, then the block one points at.
* ``search_deep``: one call, the graph-expanded search.
* ``get_related``: neighbours of a document.
* session start: the ``tools/list`` catalog, paid once per session before
  any call (in bytes and in tokens).

With ``--vault`` and ``--queries`` the script also reads, through a real
``get_document`` call, the primary expected document of every query in the
set, from a local stdio server over that vault. The server is the golden
launcher (``tests/golden_stdio_server.py``: no network, git refused, no real
key), and ``get_document`` never builds the index, so the vault is only read
and no deployment is touched. Combined with the per-query ``search`` tokens of
an evaluation report (``--eval-json``), that gives the real cost of the
dominant flow, find then read.

``--flow baseline`` measures the dominant flow itself, on the real vault, over a
real local server with the real embeddings provider: per query of the set, a
``search`` (``limit`` from the set, no other parameter) and a ``get_document``
of the primary expected document (``max_chars=4000``), the shape of the
contract as it stands. It reports tokens per call and per flow, how many reads
were truncated, and the coverage: the share of labeled queries whose read holds
every ``answer_terms`` literal. With the default provider it needs
``OPENAI_API_KEY`` in the environment (``--provider``, ``--extra-env`` and
``--index-dir`` work as in ``eval_retrieval.py``); ``get_document`` adds usage that
moves the ranking of later searches, so the recall of a search-only session is
measured apart (``eval_retrieval.py --stdio``) on its own copy of the index.

``--flow defaults`` measures the protocol of the ``instructions`` with every
default: per query, ``search(query, limit)`` with no other parameter and
``get_document(chunk_id=...)`` of the row :func:`pick_row` chooses, without
``max_chars``. ``--flow sections`` keeps the explicit protocol (pointer
``fields`` and ``max_chars=2000``) for comparison.

``--rerank on|off`` sends ``rerank`` with every ``search`` of a flow, so the
sections flow can be measured with the judge of the vault and without it, each
on its own copy of the index; ``--pass-env NAME`` hands the judge key to the
server by name, as in ``eval_retrieval.py``.

Usage::

    uv run python scripts/measure_tokens.py --markdown build/eval/tokens.md \
        --vault "$VAULT" --queries "$VAULT/Eval/queries.yaml" \
        --eval-json build/eval/http.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from knowledge import contract

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = PROJECT_ROOT / "tests" / "golden" / f"v{contract.CONTRACT_VERSION}"
GOLDEN_SERVER = PROJECT_ROOT / "tests" / "golden_stdio_server.py"
PLACEHOLDER_KEY = "placeholder-not-a-credential"
CATALOG = "tools_list"
#: The golden of the ``initialize`` handshake: not a tool response, measured with the catalog by ``--catalog``.
HANDSHAKE = "initialize"
DEFAULT_ENCODING = "cl100k_base"

TokenCounter = Callable[[str], int]

FLOWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("search → get_document", ("search", "get_document")),
    ("search(fields) → get_document(chunk_id)", ("search__pointers", "get_document__chunk")),
    ("search → get_document(chunk_id)", ("search", "get_document__by_chunk")),
    ("search_deep", ("search_deep",)),
    ("get_related", ("get_related",)),
)


@dataclass(frozen=True)
class Measure:
    name: str
    content_chars: int
    content_tokens: int
    wire_bytes: int
    wire_tokens: int


def _compact(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def measure_response(name: str, payload: dict[str, Any], counter: TokenCounter) -> Measure:
    text = "".join(block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text")
    wire = _compact(payload)
    return Measure(name, len(text), counter(text), len(wire.encode("utf-8")), counter(wire))


def measure_catalog(payload: dict[str, Any], counter: TokenCounter) -> Measure:
    wire = _compact(payload)
    return Measure(CATALOG, len(wire), counter(wire), len(wire.encode("utf-8")), counter(wire))


def load_goldens(golden_dir: Path = GOLDEN_DIR) -> dict[str, dict[str, Any]]:
    return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(golden_dir.glob("*.json"))}


def measure_all(goldens: dict[str, dict[str, Any]], counter: TokenCounter) -> tuple[Measure, list[Measure]]:
    catalog = measure_catalog(goldens[CATALOG], counter)
    responses = [
        measure_response(name, goldens[name], counter)
        for name in sorted(goldens)
        if name not in (CATALOG, HANDSHAKE)
    ]
    return catalog, responses


def render_markdown(catalog: Measure, responses: Sequence[Measure], encoding: str) -> str:
    by_name = {m.name: m for m in responses}
    tool_rows = [
        "| Respuesta (golden) | Texto: caracteres | Texto: tokens | Cable: bytes | Cable: tokens | Cable / texto |",
        "|---|---|---|---|---|---|",
    ]
    for m in sorted(responses, key=lambda m: -m.content_tokens):
        ratio = m.wire_tokens / m.content_tokens if m.content_tokens else 0.0
        tool_rows.append(
            f"| `{m.name}` | {m.content_chars} | {m.content_tokens} | {m.wire_bytes} | {m.wire_tokens} | "
            f"{ratio:.2f} |"
        )
    flow_rows = ["| Flujo | Llamadas | Texto: tokens | Cable: tokens |", "|---|---|---|---|"]
    for label, names in FLOWS:
        if not all(n in by_name for n in names):
            continue  # a flow a version did not record yet (``--golden-dir`` of an older one)
        parts = [by_name[n] for n in names]
        flow_rows.append(
            f"| {label} | {len(parts)} | {sum(p.content_tokens for p in parts)} | "
            f"{sum(p.wire_tokens for p in parts)} |"
        )
    flow_rows.append(
        f"| Arranque de sesión (`tools/list`) | 0 (handshake) | {catalog.content_tokens} | {catalog.wire_tokens} |"
    )
    total_text = sum(m.content_tokens for m in responses)
    total_wire = sum(m.wire_tokens for m in responses)
    return "\n\n".join(
        [
            "\n".join(tool_rows),
            f"Suma de las {len(responses)} respuestas: {total_text} tokens de texto, {total_wire} en el cable. "
            f"Catálogo `tools/list`: {catalog.wire_bytes} bytes, {catalog.wire_tokens} tokens "
            f"(tiktoken `{encoding}`).",
            "\n".join(flow_rows),
        ]
    )


@dataclass(frozen=True)
class RealReads:
    """``get_document`` on the primary expected document of each query."""

    tokens: list[int]
    truncated: int
    search_tokens: list[int]


async def read_primary_documents(vault: Path, queries: Path, counter: TokenCounter, errlog: Any) -> RealReads:
    from eval_retrieval import load_query_set, stdio_session

    qset = load_query_set(queries)
    tokens: list[int] = []
    truncated = 0
    async with stdio_session(
        vault, server_script=GOLDEN_SERVER, extra_env={"OPENAI_API_KEY": PLACEHOLDER_KEY}, errlog=errlog
    ) as call:
        for q in qset.queries:
            response = await call("get_document", {"file_path": q.expected[0]})
            tokens.append(counter("".join(response.texts)))
            items = response.items()
            if items and isinstance(items[0], dict) and items[0].get("truncated"):
                truncated += 1
    return RealReads(tokens=tokens, truncated=truncated, search_tokens=[])


@dataclass(frozen=True)
class FlowRow:
    """One query of a measured flow."""

    id: str
    search_tokens: int
    read_tokens: int
    truncated: bool
    covered: bool | None  # None when the query carries no answer terms
    read_expected: bool = True  # whether the read document is the query's primary expected one
    rerank_applied: bool | None = None  # whether the judge reordered the search; None when not evaluated


#: ``fields`` of the ``search`` call of the sections flow: the ones the
#: ``instructions`` protocol names (``knowledge.contract.INSTRUCTIONS``).
SECTION_SEARCH_FIELDS = ("file_path", "chunk_id", "section", "score")

#: ``max_chars`` of the ``get_document`` call of the sections flow.
SECTION_MAX_CHARS = 2000

#: ``max_chars`` of the ``get_document`` call of the baseline flow.
BASELINE_MAX_CHARS = 4000


def _read_body(read: Any) -> tuple[dict[str, Any], str]:
    """The ``get_document`` result and the document text it returned.

    The answer terms are matched against the text the server returned, not
    against the JSON-escaped block, so a newline never hides a literal.
    """
    items = read.items()
    document = items[0] if items and isinstance(items[0], dict) else {}
    body = document.get("content") if isinstance(document.get("content"), str) else "".join(read.texts)
    return document, body


async def run_baseline_flow(
    vault: Path,
    queries: Path,
    counter: TokenCounter,
    errlog: Any,
    *,
    index_dir: Path | None,
    provider: str = "openai",
    extra_env: dict[str, str] | None = None,
    rerank: str | None = None,
) -> list[FlowRow]:
    """``search`` then ``get_document`` of the primary expected document, per query."""
    from eval_retrieval import covers_answer, load_query_set, stdio_session

    qset = load_query_set(queries)
    rows: list[FlowRow] = []
    async with stdio_session(
        vault, index_dir=index_dir, provider=provider, server_script=None, extra_env=extra_env, errlog=errlog
    ) as call:
        for q in qset.queries:
            found = await call("search", _search_arguments(q.query, qset.k, rerank))
            read = await call("get_document", {"file_path": q.expected[0], "max_chars": BASELINE_MAX_CHARS})
            document, body = _read_body(read)
            rows.append(
                FlowRow(
                    id=q.id,
                    search_tokens=counter("".join(found.texts)),
                    read_tokens=counter("".join(read.texts)),
                    truncated=bool(document.get("truncated")),
                    covered=covers_answer(body, q) if q.answer_terms else None,
                    rerank_applied=_rerank_applied(found),
                )
            )
    return rows


def _search_arguments(query: str, limit: int, rerank: str | None, **extra: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {"query": query, "limit": limit, **extra}
    if rerank is not None:
        arguments["rerank"] = rerank
    return arguments


def _rerank_applied(found: Any) -> bool | None:
    from eval_retrieval import RERANK_META_KEY

    meta = (getattr(found, "meta", None) or {}).get(RERANK_META_KEY)
    return bool(meta.get("applied")) if isinstance(meta, dict) else None


def pick_row(rows: Sequence[Any], expected: str) -> dict[str, Any] | None:
    """The search row a client reads: the one on ``expected`` if any, else the first.

    The harness knows the answer (``expected[0]``); a model only sees the rows,
    so ``file_path`` is among the fields the protocol asks for and this choice
    is the one a reader of those rows can make.
    """
    dicts = [row for row in rows if isinstance(row, dict)]
    return next((row for row in dicts if row.get("file_path") == expected), dicts[0] if dicts else None)


async def run_sections_flow(
    vault: Path,
    queries: Path,
    counter: TokenCounter,
    errlog: Any,
    *,
    index_dir: Path | None,
    provider: str = "openai",
    extra_env: dict[str, str] | None = None,
    rerank: str | None = None,
) -> list[FlowRow]:
    """The ``instructions`` protocol: ``search`` with pointer fields, then read one section.

    Per query: ``search(query, limit, fields=SECTION_SEARCH_FIELDS)``, then
    ``get_document(file_path, chunk_id, max_chars=SECTION_MAX_CHARS)`` of the
    row :func:`pick_row` chooses. A row without a ``chunk_id`` (no query term
    in the file) is read from the top with the same ``max_chars``.
    """
    from eval_retrieval import covers_answer, load_query_set, stdio_session

    qset = load_query_set(queries)
    rows: list[FlowRow] = []
    async with stdio_session(
        vault, index_dir=index_dir, provider=provider, server_script=None, extra_env=extra_env, errlog=errlog
    ) as call:
        for q in qset.queries:
            found = await call(
                "search", _search_arguments(q.query, qset.k, rerank, fields=list(SECTION_SEARCH_FIELDS))
            )
            row = pick_row(found.items(), q.expected[0])
            arguments: dict[str, Any] = {"file_path": row["file_path"] if row else q.expected[0]}
            if row and row.get("chunk_id"):
                arguments["chunk_id"] = row["chunk_id"]
            arguments["max_chars"] = SECTION_MAX_CHARS
            read = await call("get_document", arguments)
            document, body = _read_body(read)
            rows.append(
                FlowRow(
                    id=q.id,
                    search_tokens=counter("".join(found.texts)),
                    read_tokens=counter("".join(read.texts)),
                    truncated=bool(document.get("truncated")),
                    covered=covers_answer(body, q) if q.answer_terms else None,
                    read_expected=arguments["file_path"] == q.expected[0],
                    rerank_applied=_rerank_applied(found),
                )
            )
    return rows


async def run_defaults_flow(
    vault: Path,
    queries: Path,
    counter: TokenCounter,
    errlog: Any,
    *,
    index_dir: Path | None,
    provider: str = "openai",
    extra_env: dict[str, str] | None = None,
    rerank: str | None = None,
) -> list[FlowRow]:
    """The protocol of the ``instructions`` with every default: ``search(query)``, then ``get_document(chunk_id)``.

    Per query: ``search(query, limit)`` and nothing else, then
    ``get_document(chunk_id=...)`` of the row :func:`pick_row` chooses, with
    no ``file_path`` and no ``max_chars``. A row without a ``chunk_id`` (no
    query term in the file) is read by its ``file_path`` from the top.
    """
    from eval_retrieval import covers_answer, load_query_set, stdio_session

    qset = load_query_set(queries)
    rows: list[FlowRow] = []
    async with stdio_session(
        vault, index_dir=index_dir, provider=provider, server_script=None, extra_env=extra_env, errlog=errlog
    ) as call:
        for q in qset.queries:
            found = await call("search", _search_arguments(q.query, qset.k, rerank))
            row = pick_row(found.items(), q.expected[0])
            chosen = row["file_path"] if row else q.expected[0]
            arguments = {"chunk_id": row["chunk_id"]} if row and row.get("chunk_id") else {"file_path": chosen}
            read = await call("get_document", arguments)
            document, body = _read_body(read)
            rows.append(
                FlowRow(
                    id=q.id,
                    search_tokens=counter("".join(found.texts)),
                    read_tokens=counter("".join(read.texts)),
                    truncated=bool(document.get("truncated")),
                    covered=covers_answer(body, q) if q.answer_terms else None,
                    read_expected=chosen == q.expected[0],
                    rerank_applied=_rerank_applied(found),
                )
            )
    return rows


#: What each measured flow calls, as the rows of its table name it.
FLOW_CALLS: dict[str, tuple[str, str]] = {
    "baseline": (
        "`search` (`limit=5`)",
        f"`get_document` del documento principal esperado (`max_chars={BASELINE_MAX_CHARS}`)",
    ),
    "sections": (
        f"`search` (`limit=5`, `fields={list(SECTION_SEARCH_FIELDS)}`)".replace("'", '"'),
        f"`get_document(file_path, chunk_id)` de la fila elegida (`max_chars={SECTION_MAX_CHARS}`)",
    ),
    "defaults": (
        "`search` (`limit=5`, ningún otro parámetro)",
        "`get_document(chunk_id)` de la fila elegida (sin `max_chars`)",
    ),
}

FLOW_RUNNERS = {"baseline": run_baseline_flow, "sections": run_sections_flow, "defaults": run_defaults_flow}


def _stats(values: Sequence[int]) -> str:
    ordered = sorted(values)
    p95 = ordered[max(0, round(0.95 * len(ordered) + 0.5) - 1)]
    return f"{statistics.fmean(ordered):.0f} · {statistics.median(ordered):.0f} · {p95}"


def render_flow(rows: Sequence[FlowRow], label: str, flow_name: str = "baseline") -> str:
    """The flow table: tokens per call and per flow, truncation and coverage."""
    labeled = [r for r in rows if r.covered is not None]
    covered = sum(1 for r in labeled if r.covered)
    flow = [r.search_tokens + r.read_tokens for r in rows]
    search_call, read_call = FLOW_CALLS[flow_name]
    table = [
        f"| {label} | Consultas | Tokens (media · mediana · p95) |",
        "|---|---|---|",
        f"| {search_call} | {len(rows)} | {_stats([r.search_tokens for r in rows])} |",
        f"| {read_call} | {len(rows)} | {_stats([r.read_tokens for r in rows])} |",
        f"| Flujo `search → get_document` | {len(rows)} | {_stats(flow)} |",
    ]
    truncated = sum(1 for r in rows if r.truncated)
    coverage = covered / len(labeled) if labeled else 0.0
    other = sum(1 for r in rows if not r.read_expected)
    elsewhere = f" Lecturas de otro documento que el principal esperado: {other}." if flow_name != "baseline" else ""
    judged = [r.rerank_applied for r in rows if r.rerank_applied is not None]
    reranked = f" Búsquedas reordenadas por el juez: {sum(judged)} de {len(judged)}." if judged else ""
    return "\n".join(table) + (
        f"\n\nMedia exacta del flujo: {statistics.fmean(flow):.1f}. "
        f"Lecturas truncadas: {truncated} de {len(rows)}.{elsewhere} "
        f"Cobertura (lecturas con todos los `answer_terms`): {covered} de {len(labeled)} ({coverage:.3f})."
        f"{reranked}"
    )


@dataclass(frozen=True)
class CatalogSize:
    """What a session pays before its first call, under one output mode."""

    structured: bool
    catalog_bytes: int
    catalog_tokens: int
    instructions_chars: int
    instructions_tokens: int


async def fetch_handshake(structured: bool) -> tuple[str, dict[str, Any]]:
    """``instructions`` and the ``tools/list`` result of the golden launcher, as wire JSON."""
    import tempfile
    from datetime import timedelta

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryDirectory(prefix="knowledge-catalog-") as tmp:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "VAULT_PATH": str(PROJECT_ROOT / "tests" / "fixtures" / "vault"),
            "KNOWLEDGE_INDEX_DIR": str(Path(tmp) / "index"),
            "OPENAI_API_KEY": PLACEHOLDER_KEY,
            "MCP_TRANSPORT": "stdio",
            "KNOWLEDGE_STRUCTURED_OUTPUT": "1" if structured else "0",
            "KNOWLEDGE_TEXT_INDENT": "",
        }
        params = StdioServerParameters(
            command=sys.executable, args=[str(GOLDEN_SERVER)], env=env, cwd=str(PROJECT_ROOT)
        )
        with open(Path(tmp) / "server.log", "w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as session:
                    init = await session.initialize()
                    tools = await session.list_tools()
    return init.instructions or "", tools.model_dump(mode="json", by_alias=True, exclude_none=True)


def measure_handshake(
    structured: bool, instructions: str, catalog: dict[str, Any], counter: TokenCounter
) -> CatalogSize:
    wire = _compact(catalog)
    return CatalogSize(structured, len(wire.encode("utf-8")), counter(wire), len(instructions), counter(instructions))


def render_catalog(sizes: Sequence[CatalogSize], encoding: str) -> str:
    rows = [
        "| `KNOWLEDGE_STRUCTURED_OUTPUT` | `tools/list`: bytes | `tools/list`: tokens | `instructions`: caracteres | "
        "`instructions`: tokens | Arranque: tokens |",
        "|---|---|---|---|---|---|",
    ]
    for size in sizes:
        rows.append(
            f"| `{1 if size.structured else 0}` | {size.catalog_bytes} | {size.catalog_tokens} | "
            f"{size.instructions_chars} | {size.instructions_tokens} | "
            f"{size.catalog_tokens + size.instructions_tokens} |"
        )
    return "\n".join(rows) + f"\n\nTokens con tiktoken `{encoding}`, catálogo en JSON compacto."


def render_real_reads(reads: RealReads, vault: str) -> str:
    def stats(values: Sequence[int]) -> str:
        ordered = sorted(values)
        p95 = ordered[max(0, round(0.95 * len(ordered) + 0.5) - 1)]
        return f"{statistics.fmean(ordered):.0f} · {statistics.median(ordered):.0f} · {p95}"

    rows = [
        "| Medición sobre el vault real | Consultas | Tokens (media · mediana · p95) |",
        "|---|---|---|",
        f"| `get_document` del documento principal esperado (`max_chars=4000`) | {len(reads.tokens)} | "
        f"{stats(reads.tokens)} |",
    ]
    if reads.search_tokens:
        rows.append(f"| `search` (`limit=5`), del reporte de evaluación | {len(reads.search_tokens)} | "
                    f"{stats(reads.search_tokens)} |")
        flow = [s + d for s, d in zip(reads.search_tokens, reads.tokens)]
        rows.append(f"| Flujo `search → get_document` | {len(flow)} | {stats(flow)} |")
    return "\n".join(rows) + (
        f"\n\nVault `{vault}`: {reads.truncated} de {len(reads.tokens)} lecturas llegaron truncadas a 4.000 "
        "caracteres (`truncated: true`), es decir, el documento completo exigiría paginar."
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--golden-dir", type=Path, default=GOLDEN_DIR)
    parser.add_argument("--encoding", default=DEFAULT_ENCODING)
    parser.add_argument("--markdown", type=Path, help="write the tables to this file")
    parser.add_argument("--vault", type=Path, help="also read each query's primary document from this vault")
    parser.add_argument("--queries", type=Path, help="query set whose primary documents are read")
    parser.add_argument("--eval-json", type=Path, help="evaluation report with per-query search tokens")
    parser.add_argument(
        "--flow",
        choices=tuple(FLOW_RUNNERS),
        help="measure a flow on the real vault with a real local server (needs --vault, --queries)",
    )
    parser.add_argument(
        "--catalog",
        action="store_true",
        help="measure tools/list and instructions with structured output on and off (fixture vault, no network)",
    )
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from eval_retrieval import add_server_arguments

    add_server_arguments(parser, "--flow")
    parser.add_argument(
        "--rerank",
        choices=("on", "off"),
        help="send rerank=on|off with every search of --flow (default: not sent)",
    )
    parser.add_argument("--json", type=Path, help="write the --flow rows as JSON")
    parser.add_argument("--server-log", type=Path, help="file for the --flow server's stderr (default: discarded)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from eval_retrieval import EvalError, server_env, tiktoken_counter

    counter = tiktoken_counter(args.encoding)
    if args.catalog:
        sizes = [
            measure_handshake(structured, *asyncio.run(fetch_handshake(structured)), counter)
            for structured in (True, False)
        ]
        rendered = render_catalog(sizes, args.encoding)
        if args.markdown:
            args.markdown.parent.mkdir(parents=True, exist_ok=True)
            args.markdown.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0
    if args.flow:
        if not (args.vault and args.queries):
            print("measure_tokens: --flow needs --vault and --queries", file=sys.stderr)
            return 2
        try:
            extra_env = server_env(args)
        except EvalError as exc:
            print(f"measure_tokens: {exc}", file=sys.stderr)
            return 2
        with open(args.server_log or os.devnull, "w", encoding="utf-8") as errlog:
            runner = FLOW_RUNNERS[args.flow]
            rows = asyncio.run(
                runner(
                    args.vault,
                    args.queries,
                    counter,
                    errlog,
                    index_dir=args.index_dir,
                    provider=args.provider,
                    extra_env=extra_env,
                    rerank=args.rerank,
                )
            )
        rendered = render_flow(rows, f"Flujo `{args.flow}` ({args.encoding})", args.flow)
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps([r.__dict__ for r in rows], indent=2) + "\n", encoding="utf-8")
        if args.markdown:
            args.markdown.parent.mkdir(parents=True, exist_ok=True)
            args.markdown.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0
    catalog, responses = measure_all(load_goldens(args.golden_dir), counter)
    rendered = render_markdown(catalog, responses, args.encoding)
    if args.vault and args.queries:
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            reads = asyncio.run(read_primary_documents(args.vault, args.queries, counter, errlog))
        if args.eval_json:
            report = json.loads(args.eval_json.read_text(encoding="utf-8"))
            by_id = {r["id"]: r["tokens"] for r in report["results"]}
            from eval_retrieval import load_query_set

            ids = [q.id for q in load_query_set(args.queries).queries]
            reads = RealReads(reads.tokens, reads.truncated, [by_id[i] for i in ids])
        rendered += "\n\n" + render_real_reads(reads, args.vault.name)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
