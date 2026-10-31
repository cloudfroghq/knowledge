"""Start-up commands under pyperf (optional): calibrated runs, worker processes, ``compare_to``.

Run by ``benchmarks/run.py`` only when ``pyperf`` imports, which the owner's
ruling allows through ``uv run --with pyperf==2.10.0 ...`` and never through the
lock file. It reads its commands from the JSON file named by ``BENCH_PYPERF_SPEC``
(pyperf re-executes this script for every worker, so the spec travels in the
environment) and, in the master process, writes a small summary next to pyperf's
own ``-o`` file::

    {"commands": {"<name>": ["python", "-c", ...]}, "summary": "<path>"}

The ``-o`` file is a regular pyperf suite, so two of them compare with
``python -m pyperf compare_to base.json head.json``.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path

import pyperf


def main() -> int:
    spec = json.loads(Path(os.environ["BENCH_PYPERF_SPEC"]).read_text(encoding="utf-8"))
    runner = pyperf.Runner()
    summary: dict[str, dict[str, object]] = {}
    for name, command in spec["commands"].items():
        bench = runner.bench_command(name, command)
        if bench is None or runner.args.worker:  # a worker measured and reported to the master
            continue
        values = [v * 1000.0 for v in bench.get_values()]
        centre = statistics.median(values)
        summary[name] = {
            "unit": "ms",
            "n": len(values),
            "p50": round(centre, 4),
            "p95": None,
            "p99": None,
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "mean": round(statistics.fmean(values), 4),
            "mad": round(statistics.median(abs(v - centre) for v in values), 4),
            "stdev": round(bench.stdev() * 1000.0, 4),
            "runs": bench.get_nrun(),
            "source": "pyperf",
        }
    if summary:
        Path(spec["summary"]).write_text(json.dumps(summary), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
