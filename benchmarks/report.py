"""Markdown rendering: the run summary, the SLO check and the base/head comparison."""

from __future__ import annotations

import fnmatch
from typing import Any

#: The proposed SLOs of docs/performance/improvement-plan.md, as
#: ``(scenario glob, metric, limit, unit)``. They are targets for the harness, not alerts, and the
#: plan states them for the reference runner and a calibrated query set: on a shared machine, or
#: with unselective queries, a miss is a pointer, not a verdict.
SLOS: list[tuple[str, str, float, str]] = [
    ("d5000.search.hybrid.*", "p50", 35.0, "ms"),
    ("d5000.search.hybrid.*", "p95", 60.0, "ms"),
    ("d5000.search.hybrid.*", "p99", 100.0, "ms"),
    ("d5000.get_document.file_path", "p99", 5.0, "ms"),
    ("d5000.context_pack.*", "p50", 45.0, "ms"),
    ("d5000.context_pack.*", "p95", 80.0, "ms"),
    ("d5000.search_deep.*", "p50", 80.0, "ms"),
    ("d5000.search_deep.*", "p95", 150.0, "ms"),
    ("d500.search.hybrid.*", "p50", 8.0, "ms"),
    ("d500.search.hybrid.*", "p95", 20.0, "ms"),
    ("d5000.index.full", "p50", 8000.0, "ms"),
    ("d5000.index.noop", "p50", 600.0, "ms"),
    ("d*.usage.flush.h*", "p50", 5.0, "ms"),
    ("startup.initialize", "p95", 650.0, "ms"),
    ("startup.doctor_brief", "p50", 300.0, "ms"),
    ("d5000.memory.rss_after_tools", "p50", 250.0, "MiB"),
]


def _fmt(value: Any, unit: str = "ms") -> str:
    if value is None:
        return "-"
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def _unit_suffix(unit: str) -> str:
    return "" if unit == "ms" else f" {unit}"


def _group_key(name: str) -> str:
    head = name.split(".", 1)[0]
    return head if head.startswith("d") and head[1:].isdigit() else "startup"


def render_summary(doc: dict[str, Any]) -> str:
    meta, results = doc["meta"], doc["results"]
    lines = ["# Benchmark summary", ""]
    lines.append(
        f"Indicative numbers: wall clock on a shared machine, commit `{meta.get('commit')}`"
        f"{' (dirty)' if meta.get('checkout_dirty') else ''}, label `{meta.get('label')}`."
    )
    lines += ["", "## Machine", ""]
    keys = [
        "cpu",
        "nproc",
        "mem_gib",
        "platform",
        "python",
        "python_build",
        "sqlite",
        "sqlite_vec",
        "vector_backend",
        "uv",
        "pyperf",
        "loadavg_start",
        "loadavg_end",
        "stats_engine",
    ]
    for key in keys:
        if meta.get(key) is not None:
            lines.append(f"- {key}: {meta[key]}")
    for docs, load in sorted(meta.get("workloads", {}).items(), key=lambda kv: int(kv[0])):
        lines.append(
            f"- vault {docs} docs: {load.get('chunks', '?')} chunks, {load.get('mib', '?')} MiB of Markdown, "
            f"seed {meta['args']['seed']}, tree `{str(load.get('tree_sha256', ''))[:12]}`"
        )
    args = meta["args"]
    lines.append(
        f"- parameters: calls {args['calls']}, warmup {args['warmup']}, selectivity {args['selectivity']}, "
        f"terms {args['terms']}, modes {args['modes']}"
    )

    groups: dict[str, list[str]] = {}
    for name in sorted(results, key=_sort_key):
        groups.setdefault(_group_key(name), []).append(name)
    for group, names in sorted(groups.items(), key=lambda kv: (kv[0] == "startup", _docs_of(kv[0]))):
        title = f"Vault of {group[1:]} documents" if group != "startup" else "Processes (start-up, stdio)"
        lines += [
            "",
            f"## {title}",
            "",
            "| scenario | n | p50 | p95 | p99 | MAD | unit |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
        for name in names:
            rec = results[name]
            unit = rec.get("unit", "ms")
            lines.append(
                f"| `{name}` | {rec['n']} | {_fmt(rec['p50'], unit)} | {_fmt(rec.get('p95'), unit)} | "
                f"{_fmt(rec.get('p99'), unit)} | {_fmt(rec.get('mad'), unit)} | {unit} |"
            )
    slo = render_slos(results)
    if slo:
        lines += ["", slo]
    notes = doc.get("notes") or []
    if notes:
        lines += ["", "## Notes", ""] + [f"- {note}" for note in notes]
    lines += [
        "",
        "`-` means the sample was too small for that percentile (p95 needs 20 samples, p99 needs 100).",
        "Where a scenario ran a few times only (index builds, process starts), read p50 as the median",
        "and MAD as the spread.",
        "",
    ]
    return "\n".join(lines)


def _docs_of(group: str) -> int:
    return int(group[1:]) if group != "startup" else 0


def _sort_key(name: str) -> tuple[int, str]:
    return (_docs_of(_group_key(name)), name)


def render_slos(results: dict[str, Any]) -> str:
    rows = []
    for pattern, metric, limit, unit in SLOS:
        for name in sorted(n for n in results if fnmatch.fnmatchcase(n, pattern)):
            rec = results[name]
            if rec.get("unit", "ms") != unit:
                continue
            value = rec.get(metric)
            shown = metric
            if value is None:  # too few samples for this percentile: the worst sample is a fair upper bound
                value, shown = rec.get("max"), f"{metric} (max of {rec['n']})"
            status = "ok" if value <= limit else "over"
            rows.append(
                f"| `{name}` | {shown} | {_fmt(value, unit)} | {_fmt(limit, unit)}{_unit_suffix(unit)} | {status} |"
            )
    if not rows:
        return ""
    head = [
        "## Against the proposed SLOs",
        "",
        "Targets from `docs/performance/improvement-plan.md`, set for the reference runner and a calibrated query set.",
        "",
        "| scenario | metric | measured | limit | |",
        "|---|---|---:|---:|---|",
    ]
    return "\n".join(head + rows)


def render_comparison(base: dict[str, Any], head: dict[str, Any], threshold: float) -> str:
    bm, hm = base["meta"], head["meta"]
    lines = [
        "# Benchmark comparison",
        "",
        _describe("base", bm),
        _describe("head", hm),
        f"- flag threshold: {threshold:.0%} on p50 (wall clock on a shared machine is good to about 10-15 %)",
    ]
    if bm.get("cpu") != hm.get("cpu") or bm.get("nproc") != hm.get("nproc"):
        lines.append("- WARNING: the two runs are from different machines; do not read the deltas as code effects")
    lines += [
        "",
        "| scenario | base p50 | head p50 | Δ p50 | base p95 | head p95 | Δ p95 | |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    common = sorted((n for n in base["results"] if n in head["results"]), key=_sort_key)
    for name in common:
        b, h = base["results"][name], head["results"][name]
        unit = b.get("unit", "ms")
        d50 = _delta(b["p50"], h["p50"])
        d95 = _delta(b.get("p95"), h.get("p95"))
        flag = ""
        if d50 is not None and abs(d50) > threshold:
            flag = "faster" if d50 < 0 else "SLOWER"
        lines.append(
            f"| `{name}` | {_fmt(b['p50'], unit)} | {_fmt(h['p50'], unit)} | {_pct(d50)} | "
            f"{_fmt(b.get('p95'), unit)} | {_fmt(h.get('p95'), unit)} | {_pct(d95)} | {flag} |"
        )
    only_base = sorted(set(base["results"]) - set(head["results"]))
    only_head = sorted(set(head["results"]) - set(base["results"]))
    if only_base:
        lines += ["", "Only in base: " + ", ".join(f"`{n}`" for n in only_base)]
    if only_head:
        lines += ["", "Only in head: " + ", ".join(f"`{n}`" for n in only_head)]
    lines.append("")
    return "\n".join(lines)


def _describe(role: str, meta: dict[str, Any]) -> str:
    dirty = " (dirty)" if meta.get("checkout_dirty") else ""
    machine = f"{meta.get('cpu')}, {meta.get('nproc')} cpus"
    return f"- {role}: commit `{meta.get('commit')}`{dirty}, label `{meta.get('label')}`, {machine}"


def _delta(base: float | None, head: float | None) -> float | None:
    if base is None or head is None or base == 0:
        return None
    return (head - base) / base


def _pct(delta: float | None) -> str:
    return "-" if delta is None else f"{delta:+.1%}"
