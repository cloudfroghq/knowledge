#!/usr/bin/env python3
"""Sweep chunk size, chunk overlap and RRF k over a labeled query set.

For every chunker configuration a fresh local server is launched over stdio
(``scripts/sweep_server.py``) against ``--vault`` with a throwaway index, so the
vault is only read and nothing deployed is touched. Each server answers three
passes over the whole query set, in this order:

1. ``search(mode="hybrid", limit=k)``: the server's own fusion
   (``0.7 · semantic + 0.3 · keyword`` times the usage boost). It runs first
   because it is the only pass the usage boost affects, and the boost moves
   with every search the process has answered.
2. ``search(mode="semantic", limit=depth)`` and
3. ``search(mode="keyword", limit=depth)``: the two ranked lists the fusion
   starts from. Reciprocal rank fusion, ``score(d) = Σ 1 / (rrf_k + rank)``, is
   computed here, in the client, for every ``rrf_k`` of the grid. Neither mode
   applies the usage boost, so the RRF rows are boost-free.

Tokens per query for an RRF row are estimated by serializing the fused top-k
rows the way the server renders them (one indented JSON block per row),
taking each row from the semantic response, or from the keyword one when the
semantic list did not return that document.

The chunk count of each configuration is read from the server's own
"Initial indexing done" log line.

Every configuration re-embeds the vault (``text-embedding-3-large`` with the
default grid: ~16 full indexings of a 151-file vault). The key comes from the
environment only::

    OPENAI_API_KEY="$(op item get OPENAI_API_KEY --fields api_key --reveal)" \\
        uv run python scripts/sweep_hyperparams.py --vault "$VAULT" \\
        --queries "$VAULT/Eval/queries.yaml" \\
        --json build/eval/sweep.json --markdown build/eval/sweep.md
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import re
import statistics
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_retrieval as ev  # noqa: E402

SWEEP_SERVER = Path(__file__).resolve().parent / "sweep_server.py"
DEFAULT_CHUNKS = (1000, 1500, 2000, 2500, 3000)
DEFAULT_OVERLAPS = (0.05, 0.10, 0.15)
DEFAULT_RRF_K = (20, 60, 100)
DEFAULT_DEPTH = 20

_INDEXED = re.compile(r"Initial indexing done: (\{.*?\})")


@dataclass(frozen=True)
class ChunkConfig:
    chunk_chars: int  # 0 = the server's own chunker
    overlap: float

    @property
    def label(self) -> str:
        return "actual (servidor)" if self.chunk_chars == 0 else f"{self.chunk_chars} · {self.overlap:.0%}"


@dataclass
class Row:
    chunker: str
    chunk_chars: int
    overlap: float
    chunks: int | None
    fusion: str
    recall: float
    precision: float
    mrr: float
    hit_rate: float
    tokens_mean: float


def rrf_fuse(lists: Sequence[Sequence[str]], rrf_k: int, top: int) -> list[str]:
    """Reciprocal rank fusion of ranked path lists; ties break by first appearance."""
    scores: dict[str, float] = {}
    order: dict[str, int] = {}
    for ranked in lists:
        for rank, path in enumerate(ranked, start=1):
            scores[path] = scores.get(path, 0.0) + 1.0 / (rrf_k + rank)
            order.setdefault(path, len(order))
    return sorted(scores, key=lambda p: (-scores[p], order[p]))[:top]


def _rows_by_path(*responses: ev.ToolResponse) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for response in responses:
        for item in response.items():
            if isinstance(item, dict) and item.get("file_path"):
                out.setdefault(item["file_path"], item)
    return out


PerQuery = list[tuple[list[str], tuple[str, ...], int]]


def _summarize(config: ChunkConfig, chunks: int | None, fusion: str, per_query: PerQuery, k: int) -> Row:
    return Row(
        chunker=config.label,
        chunk_chars=config.chunk_chars,
        overlap=config.overlap,
        chunks=chunks,
        fusion=fusion,
        recall=statistics.fmean(ev.recall_at_k(r, e, k) for r, e, _ in per_query),
        precision=statistics.fmean(ev.precision_at_k(r, e, k) for r, e, _ in per_query),
        mrr=statistics.fmean(ev.reciprocal_rank(r, e, k) for r, e, _ in per_query),
        hit_rate=statistics.fmean(1.0 if ev.reciprocal_rank(r, e, k) else 0.0 for r, e, _ in per_query),
        tokens_mean=statistics.fmean(t for _, _, t in per_query),
    )


def _chunk_count(log_path: Path) -> int | None:
    match = _INDEXED.search(log_path.read_text(encoding="utf-8", errors="replace"))
    if not match:
        return None
    try:
        return int(ast.literal_eval(match.group(1))["chunks"])
    except (ValueError, SyntaxError, KeyError, TypeError):
        return None


async def sweep_config(
    config: ChunkConfig,
    qset: ev.QuerySet,
    counter: ev.TokenCounter,
    *,
    vault: Path,
    rrf_ks: Sequence[int],
    depth: int,
    workdir: Path,
    inner: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> list[Row]:
    env = {"KNOWLEDGE_SWEEP_CHUNK_CHARS": str(config.chunk_chars), "KNOWLEDGE_SWEEP_OVERLAP": str(config.overlap)}
    if inner is not None:
        env["KNOWLEDGE_SWEEP_INNER"] = str(inner)
    env.update(extra_env or {})
    tag = f"{config.chunk_chars}-{config.overlap:.2f}"
    log_path = workdir / f"server-{tag}.log"
    k = qset.k

    hybrid: PerQuery = []
    lists: list[tuple[ev.Query, ev.ToolResponse, ev.ToolResponse]] = []
    with open(log_path, "w", encoding="utf-8") as errlog:
        async with ev.stdio_session(
            vault, index_dir=workdir / f"index-{tag}", server_script=SWEEP_SERVER, extra_env=env, errlog=errlog
        ) as search:
            for q in qset.queries:
                response = await search("search", {"query": q.query, "limit": k, "mode": "hybrid"})
                hybrid.append((ev.returned_paths(response), q.expected, counter("".join(response.texts))))
            for q in qset.queries:
                semantic = await search("search", {"query": q.query, "limit": depth, "mode": "semantic"})
                keyword = await search("search", {"query": q.query, "limit": depth, "mode": "keyword"})
                lists.append((q, semantic, keyword))

    chunks = _chunk_count(log_path)
    rows = [_summarize(config, chunks, "híbrida actual (0,7·sem + 0,3·kw)", hybrid, k)]
    for rrf_k in rrf_ks:
        per_query = []
        for q, semantic, keyword in lists:
            fused = rrf_fuse([ev.returned_paths(semantic), ev.returned_paths(keyword)], rrf_k, k)
            by_path = _rows_by_path(semantic, keyword)
            text = "".join(json.dumps(by_path[p], indent=2, ensure_ascii=False) for p in fused if p in by_path)
            per_query.append((fused, q.expected, counter(text)))
        rows.append(_summarize(config, chunks, f"RRF k={rrf_k}", per_query, k))
    return rows


def render_markdown(rows: Sequence[Row], k: int) -> str:
    lines = [
        f"| Chunker (tamaño · solape) | Chunks | Fusión | recall@{k} | precisión@{k} | MRR@{k} | acierto@{k} "
        "| tokens/consulta |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r.chunker} | {r.chunks if r.chunks is not None else '?'} | {r.fusion} | {r.recall:.3f} | "
            f"{r.precision:.3f} | {r.mrr:.3f} | {r.hit_rate:.3f} | {r.tokens_mean:.0f} |"
        )
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--chunks", type=int, nargs="+", default=list(DEFAULT_CHUNKS))
    parser.add_argument("--overlaps", type=float, nargs="+", default=list(DEFAULT_OVERLAPS))
    parser.add_argument("--rrf-k", type=int, nargs="+", default=list(DEFAULT_RRF_K))
    parser.add_argument("--depth", type=int, default=DEFAULT_DEPTH, help="list length fed to RRF per mode")
    parser.add_argument("--no-baseline", action="store_true", help="skip the server's own chunker")
    parser.add_argument("--encoding", default=ev.DEFAULT_ENCODING)
    parser.add_argument("--workdir", type=Path, help="keep indexes and server logs here (default: throwaway)")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace, workdir: Path) -> list[Row]:
    qset = ev.load_query_set(args.queries)
    counter = ev.tiktoken_counter(args.encoding)
    configs = [] if args.no_baseline else [ChunkConfig(0, 0.0)]
    configs += [ChunkConfig(c, o) for c in args.chunks for o in args.overlaps]
    rows: list[Row] = []
    for i, config in enumerate(configs, start=1):
        print(f"sweep: [{i}/{len(configs)}] chunker {config.label}", file=sys.stderr, flush=True)
        rows += await sweep_config(
            config, qset, counter, vault=args.vault, rrf_ks=args.rrf_k, depth=args.depth, workdir=workdir
        )
        if args.json:  # keep partial results if a later configuration fails
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps([asdict(r) for r in rows], indent=2, ensure_ascii=False) + "\n")
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.workdir:
            args.workdir.mkdir(parents=True, exist_ok=True)
            rows = asyncio.run(_run(args, args.workdir))
        else:
            with tempfile.TemporaryDirectory(prefix="knowledge-sweep-") as tmp:
                rows = asyncio.run(_run(args, Path(tmp)))
    except ev.EvalError as exc:
        print(f"sweep: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - one line, never a traceback with environment in it
        print(f"sweep: {type(exc).__name__}: the sweep stopped; partial rows are in --json if given", file=sys.stderr)
        return 1
    k = ev.load_query_set(args.queries).k
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(rows, k) + "\n", encoding="utf-8")
    print(render_markdown(rows, k))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
