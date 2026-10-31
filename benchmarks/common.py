"""Shared pieces of the harness: statistics, machine metadata, the benchmark environment.

Standard library only. pyperf is optional and only used for the start-up
scenarios (``benchmarks/pyperf_startup.py``); every number here comes from
``time.perf_counter`` samples summarised with percentiles, or with median and
MAD when the sample is small.
"""

from __future__ import annotations

import json
import math
import os
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

#: Repository that holds this harness (not necessarily the checkout being measured).
HARNESS_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_CONFIG = HARNESS_ROOT / "tests" / "fixtures" / "config.json"

#: Percentiles below these sample sizes are noise, so the summary leaves them out.
MIN_N_P95 = 20
MIN_N_P99 = 100


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile (``q`` in 0..100) of an ascending list."""
    if not sorted_values:
        raise ValueError("percentile of an empty sample")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * q / 100.0
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (rank - lo)


def mad(values: Iterable[float]) -> float:
    """Median absolute deviation: the robust spread reported next to a median."""
    data = list(values)
    if not data:
        raise ValueError("mad of an empty sample")
    centre = statistics.median(data)
    return statistics.median(abs(v - centre) for v in data)


def summarize(samples: Iterable[float], *, unit: str = "ms", **extra: Any) -> dict[str, Any]:
    """The result record of one scenario: ``p50`` always, ``p95`` from 20 samples, ``p99`` from 100.

    ``mad``, ``min``, ``max`` and ``mean`` are always present. ``extra`` keys are
    copied in (for example the achieved selectivity of a query set).
    """
    data = sorted(samples)
    if not data:
        raise ValueError("a scenario produced no samples")
    n = len(data)
    record: dict[str, Any] = {
        "unit": unit,
        "n": n,
        "p50": round(percentile(data, 50), 4),
        "p95": round(percentile(data, 95), 4) if n >= MIN_N_P95 else None,
        "p99": round(percentile(data, 99), 4) if n >= MIN_N_P99 else None,
        "min": round(data[0], 4),
        "max": round(data[-1], 4),
        "mean": round(statistics.fmean(data), 4),
        "mad": round(mad(data), 4),
    }
    record.update(extra)
    return record


def sample(fn: Callable[[], Any], *, runs: int, warmup: int = 0) -> list[float]:
    """Wall-clock milliseconds of ``runs`` calls of ``fn`` after ``warmup`` untimed calls."""
    for _ in range(warmup):
        fn()
    out: list[float] = []
    for _ in range(runs):
        started = time.perf_counter()
        fn()
        out.append((time.perf_counter() - started) * 1000.0)
    return out


# ---------------------------------------------------------------------------
# Machine and checkout metadata
# ---------------------------------------------------------------------------


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _mem_gib() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024 / 1024, 1)
    except (OSError, ValueError):
        pass
    return None


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _uv_version() -> str | None:
    exe = shutil.which("uv")
    if not exe:
        return None
    try:
        done = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


def checkout_info() -> dict[str, Any]:
    """Which ``knowledge`` is measured: the checkout the interpreter imports, its commit and whether it is dirty."""
    try:
        import knowledge
    except ImportError:
        return {"importable": False}
    package = Path(knowledge.__file__).resolve().parent
    info: dict[str, Any] = {"importable": True, "package": str(package)}
    top = _git(["rev-parse", "--show-toplevel"], package)
    if top:
        info["checkout"] = top
        info["commit"] = _git(["rev-parse", "--short=12", "HEAD"], package)
        info["subject"] = _git(["log", "-1", "--format=%s"], package)
        status = _git(["status", "--porcelain", "--untracked-files=no"], package)
        info["dirty"] = bool(status)
    try:
        from importlib.metadata import version

        info["version"] = version("cloudfrog-knowledge")
    except Exception:
        info["version"] = None
    return info


def machine_info() -> dict[str, Any]:
    """Everything needed to judge whether two result files are comparable."""
    info: dict[str, Any] = {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_build": " ".join(platform.python_build()),
        "executable": sys.executable,
        "platform": platform.platform(),
        "cpu": _cpu_model(),
        "nproc": os.cpu_count(),
        "mem_gib": _mem_gib(),
        "sqlite": sqlite3.sqlite_version,
        "uv": _uv_version(),
        "loadavg_start": [round(v, 2) for v in os.getloadavg()] if hasattr(os, "getloadavg") else None,
    }
    try:
        from knowledge import stores

        info["vector_backend"] = stores.probe_vector_backend()
    except Exception as exc:  # the harness still reports the machine without the package
        info["vector_backend"] = f"unavailable ({type(exc).__name__})"
    try:
        import sqlite_vec

        info["sqlite_vec"] = getattr(sqlite_vec, "__version__", None)
    except ImportError:
        info["sqlite_vec"] = None
    try:
        import pyperf

        info["pyperf"] = pyperf.__version__
    except ImportError:
        info["pyperf"] = None
    info.update({f"checkout_{k}": v for k, v in checkout_info().items()})
    return info


# ---------------------------------------------------------------------------
# The environment every measured process runs in
# ---------------------------------------------------------------------------

#: Variables that could point a measurement at a deployment, a memory server or a judge.
_CLEARED = (
    "KNOWLEDGE_HOME",
    "KNOWLEDGE_VAULTS",
    "CHROMA_PERSIST_DIR",
    "CHROMA_COLLECTION",
    "VAULT_REMOTE",
    "MCP_API_KEY",
    "MCP_ADMIN_KEY",
    "MCP_WEBHOOK_SECRET",
    "MEMORY_HTTP_TOKEN",
    "KNOWLEDGE_MEMORY_URL",
    "KNOWLEDGE_MEMORY_REQUIRE_TOKEN",
    "KNOWLEDGE_INTEGRATION_SITES",
    "KNOWLEDGE_STRUCTURED_OUTPUT",
    "KNOWLEDGE_PROFILE",
    "TYPESAFE_API_KEY",
    "KEV_API_KEY",
    "OPENAI_API_KEY",
)


def bench_env(
    vault: Path | None,
    index_dir: Path | None,
    *,
    require_vec: bool = True,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment of a measured process: bag-of-words embeddings, a temp index, no network.

    ``vault`` and ``index_dir`` may be ``None`` for a scenario that touches neither
    (the import-time probes).
    """
    env = dict(base if base is not None else os.environ)
    for name in list(env):
        if (
            name in _CLEARED
            or name.startswith("KNOWLEDGE_RERANK")
            or name.endswith(("_TYPESAFE_API_KEY", "_KEV_API_KEY"))
        ):
            env[name] = ""
        elif ("JIRA" in name or "CONFLUENCE" in name) and name.isupper():
            env[name] = ""
    env["KNOWLEDGE_EMBEDDINGS_PROVIDER"] = "bag-of-words"
    env["KNOWLEDGE_CONFIG_JSON"] = str(FIXTURE_CONFIG)
    env["MCP_TRANSPORT"] = "stdio"
    env["MEMORY_HTTP_TOKEN"] = ""
    env["KNOWLEDGE_MEMORY_URL"] = ""
    if require_vec:
        env["KNOWLEDGE_REQUIRE_SQLITE_VEC"] = "1"
    else:
        env.pop("KNOWLEDGE_REQUIRE_SQLITE_VEC", None)
    if vault is not None:
        env["VAULT_PATH"] = str(vault)
    if index_dir is not None:
        env["KNOWLEDGE_INDEX_DIR"] = str(index_dir)
    return env


def rss_mib() -> float | None:
    """Resident set size of this process (``VmRSS``), or ``None`` where /proc is absent."""
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError):
        pass
    return None


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
