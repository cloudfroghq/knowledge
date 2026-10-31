"""Benchmark runner: ``python -m benchmarks`` or ``python benchmarks/run.py``.

    run      generate synthetic vaults, run the scenarios, write results.json and summary.md
    compare  base.json head.json: a table of deltas between two runs
    info     print the machine and checkout metadata and exit

``run`` is the default subcommand. The harness measures the ``knowledge`` that the
interpreter running it imports, so a run against another checkout is

    uv run --directory "$CHECKOUT" python "$HARNESS/benchmarks/run.py" run ...

See ``docs/performance/benchmarks.md``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from benchmarks import report, startup  # noqa: E402
from benchmarks.common import HARNESS_ROOT, bench_env, dump_json, machine_info  # noqa: E402

HERE = Path(__file__).resolve().parent
PYPERF_PIN = "2.10.0"
ALL_SCENARIOS = ("index", "tools", "usage", "startup")
# One line (pyperf rejects newlines in the command it records) that ignores the exit code of the command.
_DOCTOR_NO_EXIT = (
    "exec(\"from knowledge import cli\\ntry:\\n cli.main(['doctor', '--brief'])\\nexcept SystemExit:\\n pass\")"
)


class HarnessError(RuntimeError):
    """A scenario could not run; the message says why and carries the worker's stderr tail."""


def _log(message: str) -> None:
    print(f"[bench {time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmarks", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the scenarios")
    run.add_argument(
        "--docs", default="500", help="comma list of synthetic vault sizes (default 500; the plan uses 500,5000)"
    )
    run.add_argument("--scenarios", default="all", help=f"comma list of {','.join(ALL_SCENARIOS)} or all")
    run.add_argument("--calls", type=int, default=200, help="timed calls per tool scenario (>= 200 for stable p95/p99)")
    run.add_argument("--warmup", type=int, default=10, help="untimed calls before each tool scenario")
    run.add_argument(
        "--selectivity",
        default="0.02,zipf",
        help="comma list: a document share each query word matches (0.02) or zipf (the old ~85 percent matches)",
    )
    run.add_argument("--terms", type=int, default=2, help="words per query")
    run.add_argument("--modes", default="hybrid", help="search modes to time: hybrid,keyword,semantic")
    run.add_argument("--seed", type=int, default=7, help="seed of the vault and the queries")
    run.add_argument(
        "--build-reps", type=int, default=None, help="full builds per vault (default 3 up to 1000 docs, else 1)"
    )
    run.add_argument(
        "--reps", type=int, default=None, help="no-op and one-changed reindexes (default 5 up to 1000 docs, else 3)"
    )
    run.add_argument(
        "--usage-history",
        default="100,1000",
        help="searches of recorded usage before the flush scenarios (5000 is slow)",
    )
    run.add_argument("--startup-runs", type=int, default=10, help="fresh processes per start-up probe")
    run.add_argument("--stdio-calls", type=int, default=100, help="round trips per stdio tool")
    run.add_argument(
        "--pyperf",
        choices=("auto", "on", "off"),
        default="auto",
        help="use pyperf for the start-up probes when importable",
    )
    run.add_argument("--pyperf-processes", type=int, default=6)
    run.add_argument("--pyperf-values", type=int, default=2)
    run.add_argument(
        "--no-require-vec",
        action="store_true",
        help="allow the stdlib vector fallback (default: fail without sqlite-vec)",
    )
    run.add_argument(
        "--workdir", type=Path, default=None, help="scratch directory (default: a new temp dir, removed at the end)"
    )
    run.add_argument("--keep", action="store_true", help="keep the scratch directory")
    run.add_argument("--out", type=Path, default=None, help="output directory (default .data/bench/<utc>-<label>)")
    run.add_argument("--label", default=None, help="name of this run in the output (default: the commit)")
    run.add_argument(
        "--quick", action="store_true", help="a smoke run: few calls, one rep. Numbers are not for comparison"
    )

    cmp_ = sub.add_parser("compare", help="compare two results.json files")
    cmp_.add_argument("base", type=Path)
    cmp_.add_argument("head", type=Path)
    cmp_.add_argument(
        "--threshold", type=float, default=0.15, help="flag p50 changes above this fraction (default 0.15)"
    )
    cmp_.add_argument("--out", type=Path, default=None, help="also write the table here")

    sub.add_parser("info", help="print machine and checkout metadata")
    return parser


def _csv(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _selectivities(text: str) -> list[tuple[str, float | None]]:
    out: list[tuple[str, float | None]] = []
    for token in _csv(text):
        if token.lower() in ("zipf", "none"):
            out.append(("zipf", None))
        else:
            value = float(token)
            if not 0 < value <= 1:
                raise SystemExit(f"--selectivity: {token!r} is not in (0, 1]")
            out.append((token, value))
    if not out:
        raise SystemExit("--selectivity needs at least one value")
    return out


def _apply_quick(args: argparse.Namespace) -> None:
    args.calls, args.warmup = min(args.calls, 40), min(args.warmup, 3)
    args.build_reps, args.reps = 1, 2
    args.startup_runs, args.stdio_calls = 3, 20
    args.usage_history = "100"
    args.pyperf_processes, args.pyperf_values = 2, 1


# ---------------------------------------------------------------------------
# Workloads and workers
# ---------------------------------------------------------------------------


def prepare_workload(
    workdir: Path, docs: int, args: argparse.Namespace, selectivities: list[tuple[str, float | None]]
) -> dict[str, Any]:
    """Generate the synthetic vault of ``docs`` documents under ``workdir`` and its query sets."""
    sys.path.insert(0, str(HARNESS_ROOT / "tests"))
    from synthetic_vault import generate, queries  # stdlib only: importable against any checkout

    root = workdir / f"syn{docs}"
    if root.exists():
        shutil.rmtree(root)
    started = time.perf_counter()
    manifest = generate(root / "vault", docs=docs, seed=args.seed)
    count = max(args.calls + args.warmup, 200)
    query_sets = {
        label: queries(manifest, seed=args.seed, n=count, selectivity=value, terms=args.terms)
        for label, value in selectivities
    }
    _log(f"d{docs}: generated {len(manifest.paths)} docs in {time.perf_counter() - started:.1f}s")
    return {
        "docs": docs,
        "vault": root / "vault",
        "index": root / "index",
        "scratch": root / "scratch",
        "paths": list(manifest.paths),
        "queries": query_sets,
        "mib": round(manifest.total_bytes / 2**20, 2),
        "tree_sha256": manifest.tree_sha256,
    }


def run_worker(
    group: str, work: dict[str, Any], args: argparse.Namespace, workdir: Path, **extra: Any
) -> dict[str, Any]:
    work["index"].mkdir(parents=True, exist_ok=True)
    work["scratch"].mkdir(parents=True, exist_ok=True)
    spec = {
        "vault": str(work["vault"]),
        "index": str(work["index"]),
        "scratch": str(work["scratch"]),
        "docs": work["docs"],
        "paths": work["paths"],
        "queries": work["queries"],
        "seed": args.seed,
        "calls": args.calls,
        "warmup": args.warmup,
        "modes": _csv(args.modes),
        "result": str(workdir / f"{group}-{work['docs']}-result.json"),
        **extra,
    }
    spec_path = workdir / f"{group}-{work['docs']}-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    Path(spec["result"]).unlink(missing_ok=True)
    env = bench_env(work["vault"], work["index"], require_vec=not args.no_require_vec)
    started = time.perf_counter()
    done = subprocess.run(
        [sys.executable, str(HERE / "scenarios.py"), group, "--spec", str(spec_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    outcome: dict[str, Any] = {}
    if Path(spec["result"]).exists():
        outcome = json.loads(Path(spec["result"]).read_text(encoding="utf-8"))
    if done.returncode != 0 or "error" in outcome:
        tail = "\n".join((done.stderr or "").strip().splitlines()[-25:])
        raise HarnessError(f"worker {group!r} failed for {work['docs']} docs (rc {done.returncode}):\n{tail}")
    _log(f"d{work['docs']}: {group} done in {time.perf_counter() - started:.1f}s")
    return outcome


def prefixed(docs: int, results: dict[str, Any]) -> dict[str, Any]:
    return {f"d{docs}.{name}": record for name, record in results.items()}


# ---------------------------------------------------------------------------
# Start-up scenarios
# ---------------------------------------------------------------------------


def pyperf_available() -> bool:
    return importlib.util.find_spec("pyperf") is not None


def run_pyperf_startup(args: argparse.Namespace, env: dict[str, str], workdir: Path, out_dir: Path) -> dict[str, Any]:
    py = sys.executable
    commands = {
        "startup.pyperf.import_cli": [py, "-c", "import knowledge.cli"],
        "startup.pyperf.import_server": [py, "-c", "import knowledge.server"],
        "startup.pyperf.doctor_brief": [py, "-c", _DOCTOR_NO_EXIT],
    }
    summary_path = workdir / "pyperf-summary.json"
    spec_path = workdir / "pyperf-spec.json"
    spec_path.write_text(json.dumps({"commands": commands, "summary": str(summary_path)}), encoding="utf-8")
    suite = out_dir / "pyperf-startup.json"
    suite.unlink(missing_ok=True)
    summary_path.unlink(missing_ok=True)
    child_env = dict(env)
    child_env["BENCH_PYPERF_SPEC"] = str(spec_path)
    cmd = [
        py, str(HERE / "pyperf_startup.py"), "--copy-env", "--quiet",
        "--processes", str(args.pyperf_processes), "--values", str(args.pyperf_values), "--warmups", "1",
        "-o", str(suite),
    ]  # fmt: skip
    started = time.perf_counter()
    done = subprocess.run(cmd, env=child_env, capture_output=True, text=True, check=False)
    if done.returncode != 0 or not summary_path.exists():
        raise HarnessError(
            f"pyperf start-up run failed (rc {done.returncode}):\n"
            + "\n".join((done.stderr or done.stdout).splitlines()[-15:])
        )
    _log(f"startup: pyperf done in {time.perf_counter() - started:.1f}s (suite: {suite})")
    return json.loads(summary_path.read_text(encoding="utf-8"))


def run_startup(
    args: argparse.Namespace, work: dict[str, Any], workdir: Path, out_dir: Path, notes: list[str]
) -> dict[str, Any]:
    env = bench_env(work["vault"], work["index"], require_vec=not args.no_require_vec)
    results: dict[str, Any] = {}
    runs = args.startup_runs
    results["startup.interpreter_floor"] = startup.interpreter_floor(runs, env)
    results["startup.import.knowledge.cli"] = startup.import_time("knowledge.cli", runs, env)
    results["startup.import.knowledge.server"] = startup.import_time("knowledge.server", runs, env)
    results["startup.doctor_brief"] = startup.doctor_brief(runs, env)
    _log(f"startup: import and doctor probes done ({runs} runs each)")
    results["startup.initialize"], results["startup.tools_list_first"] = startup.initialize_time(runs, env)
    _log("startup: initialize handshake done")
    # The stdio search needs an index to exist; the index group built it, and ensure-index covers the rest.
    run_worker("ensure-index", work, args, workdir)
    results.update(startup.stdio_round_trips(args.stdio_calls, next(iter(work["queries"].values())), env))
    _log("startup: stdio round trips done")
    use_pyperf = args.pyperf == "on" or (args.pyperf == "auto" and pyperf_available())
    if args.pyperf == "on" and not pyperf_available():
        raise HarnessError(
            f"--pyperf on needs pyperf: uv run --with pyperf=={PYPERF_PIN} python benchmarks/run.py run ..."
        )
    if use_pyperf:
        results.update(run_pyperf_startup(args, env, workdir, out_dir))
    else:
        notes.append(
            "pyperf not used for the start-up probes (not importable); "
            f"rerun with `uv run --with pyperf=={PYPERF_PIN} ...` for calibrated process runs"
        )
    return results


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    if args.quick:
        _apply_quick(args)
    if args.calls < 200 and not args.quick:
        _log("warning: fewer than 200 calls per scenario; p95 and p99 are indicative only")
    scenarios = list(ALL_SCENARIOS) if args.scenarios == "all" else _csv(args.scenarios)
    unknown = sorted(set(scenarios) - set(ALL_SCENARIOS))
    if unknown:
        raise SystemExit(f"unknown scenario(s): {', '.join(unknown)}; choose from {', '.join(ALL_SCENARIOS)}")
    sizes = sorted({int(d) for d in _csv(args.docs)})
    selectivities = _selectivities(args.selectivity)
    history = [int(h) for h in _csv(args.usage_history)]

    info = machine_info()
    if info.get("checkout_importable") is False:
        raise SystemExit("the 'knowledge' package is not importable here; run with `uv run` from a checkout")
    label = args.label or info.get("checkout_commit") or "run"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out or (HARNESS_ROOT / ".data" / "bench" / f"{stamp}-{label}")
    out_dir.mkdir(parents=True, exist_ok=True)
    own_workdir = args.workdir is None
    workdir = Path(tempfile.mkdtemp(prefix="knowledge-bench-")) if own_workdir else args.workdir
    workdir.mkdir(parents=True, exist_ok=True)

    info["commit"] = info.get("checkout_commit")
    info["label"] = label
    info["started"] = stamp
    info["stats_engine"] = "perf_counter median/MAD/percentiles" + (
        " + pyperf (start-up)" if pyperf_available() and args.pyperf != "off" else ""
    )
    info["args"] = {
        "docs": sizes,
        "scenarios": scenarios,
        "calls": args.calls,
        "warmup": args.warmup,
        "selectivity": [s for s, _ in selectivities],
        "terms": args.terms,
        "modes": _csv(args.modes),
        "seed": args.seed,
        "quick": args.quick,
    }
    print(json.dumps({k: v for k, v in info.items() if k != "args"}, indent=2, sort_keys=True), file=sys.stderr)

    results: dict[str, Any] = {}
    notes: list[str] = []
    info["workloads"] = {}
    try:
        contexts: dict[int, dict[str, Any]] = {}
        for docs in sizes:
            work = prepare_workload(workdir, docs, args, selectivities)
            contexts[docs] = work
            info["workloads"][str(docs)] = {"mib": work["mib"], "tree_sha256": work["tree_sha256"]}
            build_reps = args.build_reps if args.build_reps is not None else (3 if docs <= 1000 else 1)
            reps = args.reps if args.reps is not None else (5 if docs <= 1000 else 3)
            if "index" in scenarios:
                outcome = run_worker("index", work, args, workdir, build_reps=build_reps, reps=reps)
                results.update(prefixed(docs, outcome["results"]))
                info["workloads"][str(docs)].update(outcome.get("extra", {}))
            if "tools" in scenarios:
                run_worker("ensure-index", work, args, workdir)
                outcome = run_worker("tools", work, args, workdir)
                results.update(prefixed(docs, outcome["results"]))
            if "usage" in scenarios:
                outcome = run_worker(
                    "usage", work, args, workdir, usage_history=history, flush_reps=10, record_calls=200
                )
                results.update(prefixed(docs, outcome["results"]))
        if "startup" in scenarios:
            results.update(run_startup(args, contexts[sizes[0]], workdir, out_dir, notes))
    finally:
        if own_workdir and not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)

    info["loadavg_end"] = [round(v, 2) for v in os.getloadavg()]
    doc = {"schema": 1, "meta": info, "results": results, "notes": notes}
    dump_json(out_dir / "results.json", doc)
    summary = report.render_summary(doc)
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    _log(f"wrote {out_dir / 'results.json'} and {out_dir / 'summary.md'}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    base = json.loads(args.base.read_text(encoding="utf-8"))
    head = json.loads(args.head.read_text(encoding="utf-8"))
    table = report.render_comparison(base, head, args.threshold)
    if args.out:
        args.out.write_text(table, encoding="utf-8")
    print(table)
    return 0


def cmd_info(_: argparse.Namespace) -> int:
    print(json.dumps(machine_info(), indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("run", "compare", "info", "-h", "--help"):
        argv = ["run", *argv]
    args = build_parser().parse_args(argv)
    try:
        return {"run": cmd_run, "compare": cmd_compare, "info": cmd_info}[args.command](args)
    except HarnessError as exc:
        print(f"benchmarks: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
