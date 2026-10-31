"""Tool-call telemetry: how many ``tools/call`` each tool received, from which client and principal.

The counts live in section ``tool_calls`` of the default vault's
``usage_stats.json``. Pinned here: the layout on disk, the labels (never an
argument, a query or a bearer), the 120-day retention, the merge with the
counts other processes wrote (a stale process never lowers them), the server
subclass that counts every call without touching its response, and the
``doctor`` block that reads them back.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, Implementation

from knowledge import cli, contract, principals, server, settings, usage
from knowledge.usage import UsageTracker

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures" / "vault"
API_TOKEN = "service-not-a-credential-service"


def _on_disk(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())["tool_calls"]["days"]


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "usage_stats.json"


# ---------------------------------------------------------------------------
# The tracker
# ---------------------------------------------------------------------------


def test_a_call_is_counted_by_day_tool_client_and_principal(path: Path) -> None:
    tracker = UsageTracker(path)
    tracker.record_call("search", client="claude-code", principal="stdio", error=False)
    tracker.record_call("search", client="claude-code", principal="stdio", error=True)
    tracker.record_call("get_document", client="claude-ai", principal="service", error=False)
    tracker.flush()

    assert _on_disk(path) == {
        usage.utc_day(): {
            "get_document": {"claude-ai": {"service": {"calls": 1, "errors": 0}}},
            "search": {"claude-code": {"stdio": {"calls": 2, "errors": 1}}},
        }
    }
    assert tracker.pending_tool_calls() == {}


def test_nothing_but_names_and_counts_reaches_the_file(path: Path) -> None:
    tracker = UsageTracker(path)
    tracker.record_call("search", client="Claude Desktop/1.0 (macOS)", principal=None, error=False)
    tracker.record_call("", client="", principal="", error=False)
    tracker.flush()

    day = _on_disk(path)[usage.utc_day()]
    assert day == {
        "search": {"Claude_Desktop_1.0__macOS_": {"unknown": {"calls": 1, "errors": 0}}},
        "unknown": {"unknown": {"unknown": {"calls": 1, "errors": 0}}},
    }


def test_labels_are_cut_and_names_past_the_daily_caps_fold_into_other(path: Path) -> None:
    assert usage.call_label("x" * 100) == "x" * 40
    assert usage.call_label("x" * 100, max_len=64) == "x" * 64
    assert usage.call_label(None) == usage.UNKNOWN_LABEL
    tracker = UsageTracker(path)
    for n in range(70):
        tracker.record_call(f"tool-{n:02d}", client="c", principal="p", error=False)
    tracker.flush()

    day = _on_disk(path)[usage.utc_day()]
    assert len(day) == 65
    assert day[usage.OTHER_LABEL] == {"c": {"p": {"calls": 6, "errors": 0}}}


def test_counts_flush_with_the_rest_of_the_usage(path: Path) -> None:
    tracker = UsageTracker(path)
    for _ in range(19):
        tracker.record_call("search", client="c", principal="p", error=False)
    assert not path.exists()

    tracker.record_call("search", client="c", principal="p", error=False)

    assert _on_disk(path)[usage.utc_day()]["search"]["c"]["p"]["calls"] == 20


def test_two_trackers_over_one_file_add_their_calls(path: Path) -> None:
    """Two trackers over one ``usage_stats.json`` stand for two processes over one vault."""
    first, second = UsageTracker(path), UsageTracker(path)
    first.record_call("search", client="claude-code", principal="stdio", error=False)
    second.record_call("search", client="claude-code", principal="stdio", error=False)
    second.record_call("list_vaults", client="claude-ai", principal="stdio", error=False)

    first.flush()
    second.flush()
    first.flush()  # nothing pending: rewriting the file keeps what the other process wrote

    day = _on_disk(path)[usage.utc_day()]
    assert day["search"]["claude-code"]["stdio"] == {"calls": 2, "errors": 0}
    assert day["list_vaults"]["claude-ai"]["stdio"] == {"calls": 1, "errors": 0}


def test_a_stale_tracker_never_lowers_the_calls_on_disk(path: Path) -> None:
    """A process that started before the others wrote merges with the file instead of overwriting it."""
    stale = UsageTracker(path)
    other = UsageTracker(path)
    for _ in range(5):
        other.record_call("search", client="c", principal="p", error=False)
    other.flush()

    stale.record_uses(["a.md"])  # a write about something else entirely
    stale.flush()

    assert _on_disk(path)[usage.utc_day()]["search"]["c"]["p"]["calls"] == 5
    assert UsageTracker(path).uses("a.md") == 1.0


def test_a_paid_rerank_write_carries_the_pending_calls(path: Path) -> None:
    tracker = UsageTracker(path)
    tracker.record_call("search", client="c", principal="p", error=False)

    tracker.record_rerank(applied=True, reason=None, usd=0.01)

    assert _on_disk(path)[usage.utc_day()]["search"]["c"]["p"]["calls"] == 1
    assert tracker.pending_tool_calls() == {}


_WORKER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from knowledge.usage import UsageTracker

    tracker = UsageTracker(Path(sys.argv[1]))
    for n in range(int(sys.argv[2])):
        tracker.record_call("search", client=sys.argv[3], principal="stdio", error=n % 10 == 0)
        if n % 7 == 0:
            tracker.flush()
    tracker.flush()
    """
)


def test_two_processes_writing_at_once_lose_no_call(path: Path) -> None:
    workers = [
        subprocess.Popen([sys.executable, "-c", _WORKER, str(path), "150", client])
        for client in ("claude-code", "claude-ai")
    ]
    assert [w.wait(timeout=120) for w in workers] == [0, 0]

    day = _on_disk(path)[usage.utc_day()]["search"]
    assert day == {
        "claude-ai": {"stdio": {"calls": 150, "errors": 15}},
        "claude-code": {"stdio": {"calls": 150, "errors": 15}},
    }
    assert not list(path.parent.glob("*.tmp"))


def test_days_older_than_the_retention_are_dropped(path: Path) -> None:
    today = date.fromisoformat(usage.utc_day())
    kept = (today - timedelta(days=usage.TOOL_CALL_DAYS_KEPT - 1)).isoformat()
    dropped = (today - timedelta(days=usage.TOOL_CALL_DAYS_KEPT)).isoformat()
    leaf = {"search": {"c": {"p": {"calls": 3, "errors": 0}}}}
    path.write_text(json.dumps({"tool_calls": {"days": {dropped: leaf, kept: leaf}}}))

    tracker = UsageTracker(path)
    tracker.record_call("search", client="c", principal="p", error=False)
    tracker.flush()

    assert list(_on_disk(path)) == [kept, today.isoformat()]


def test_a_malformed_section_reads_as_no_calls(path: Path) -> None:
    path.write_text(json.dumps({"tool_calls": {"days": {"2026-10-01": {"search": {"c": {"p": "many"}}}}}}))
    tracker = UsageTracker(path)
    tracker.record_call("search", client="c", principal="p", error=False)
    tracker.flush()

    assert list(_on_disk(path)) == [usage.utc_day()]


def test_the_summary_reports_windows_deprecated_and_unregistered_names(path: Path) -> None:
    today = date(2026, 10, 12)
    days = {
        (today - timedelta(days=40)).isoformat(): {"search_code": {"claude-code": {"stdio": {"calls": 2, "errors": 0}}}},
        (today - timedelta(days=3)).isoformat(): {"search": {"claude-ai": {"service": {"calls": 4, "errors": 1}}}},
        today.isoformat(): {
            "search": {"claude-code": {"stdio": {"calls": 1, "errors": 0}}},
            "made_up": {"probe": {"stdio": {"calls": 1, "errors": 1}}},
        },
    }
    path.write_text(json.dumps({"tool_calls": {"days": days}}))

    summary = usage.tool_calls_summary(
        path,
        day=today.isoformat(),
        registered=frozenset({"search", "search_code"}),
        deprecated=frozenset({"search_code"}),
    )

    assert summary["first_day"] == "2026-09-02" and summary["last_day"] == "2026-10-12"
    assert summary["last_7_days"] == {
        "calls": 6,
        "errors": 2,
        "by_tool": {"made_up": 1, "search": 5},
        "by_client": {"claude-ai": 4, "claude-code": 1, "probe": 1},
        "by_principal": {"service": 4, "stdio": 2},
    }
    assert summary["last_30_days"]["calls"] == 6
    assert summary["kept"]["calls"] == 8
    assert summary["deprecated"] == {"search_code": 2}
    assert summary["unregistered"] == {"made_up": 1}


def test_the_summary_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    summary = usage.tool_calls_summary(tmp_path / "absent.json")

    assert summary["kept"]["calls"] == 0
    assert summary["first_day"] is None and summary["deprecated"] == {} and summary["unregistered"] == {}


# ---------------------------------------------------------------------------
# The server counts every call, and nothing else changes
# ---------------------------------------------------------------------------


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> UsageTracker:
    """The default runtime, open, with a fresh tracker in place of its own."""
    rt = server.default_runtime()
    tracker = UsageTracker(tmp_path / "usage_stats.json")
    monkeypatch.setattr(rt, "usage", tracker)
    return tracker


def _app(cls: type[FastMCP]) -> FastMCP:
    app = cls("calls-probe")
    server.register_tools(app, structured=True)
    return app


async def _session_calls(app: FastMCP, client: str, calls: list[tuple[str, dict[str, Any]]]) -> list[CallToolResult]:
    async with create_connected_server_and_client_session(
        app, client_info=Implementation(name=client, version="0")
    ) as session:
        return [await session.call_tool(name, arguments) for name, arguments in calls]


def test_the_server_counts_each_call_with_its_client(counted: UsageTracker) -> None:
    calls = [
        ("list_vaults", {}),
        ("list_vaults", {}),
        ("search", {"query": "middleware", "fields": ["no_such_field"]}),
        ("no_such_tool", {}),
    ]

    results = asyncio.run(_session_calls(_app(server.KnowledgeMCP), "claude-code", calls))
    server.drain_tool_call_counts(timeout=10)

    assert [r.isError for r in results] == [False, False, True, True]
    assert counted.pending_tool_calls() == {
        usage.utc_day(): {
            "list_vaults": {"claude-code": {"stdio": {"calls": 2, "errors": 0}}},
            "no_such_tool": {"claude-code": {"stdio": {"calls": 1, "errors": 1}}},
            "search": {"claude-code": {"stdio": {"calls": 1, "errors": 1}}},
        }
    }


def test_a_retired_or_unloaded_tool_is_answered_with_a_code_and_counted(counted: UsageTracker) -> None:
    """A cached 1.x catalog calls ``search_code`` or ``get_flow``: the answer carries a code and the count sees it."""
    calls = [("search_code", {"query": "anything"}), ("get_flow", {"provider": "Telvia"})]

    retired, unloaded = asyncio.run(_session_calls(_app(server.KnowledgeMCP), "claude-code", calls))
    server.drain_tool_call_counts(timeout=10)

    assert retired.isError is True and retired.content[0].text.startswith("E_TOOL_RETIRED: search_code was retired")
    assert unloaded.isError is True and unloaded.content[0].text.startswith("E_FEATURE_DISABLED: ")
    assert counted.pending_tool_calls() == {
        usage.utc_day(): {
            "get_flow": {"claude-code": {"stdio": {"calls": 1, "errors": 1}}},
            "search_code": {"claude-code": {"stdio": {"calls": 1, "errors": 1}}},
        }
    }


def test_counting_leaves_every_response_as_fastmcp_builds_it(counted: UsageTracker) -> None:
    calls = [("list_vaults", {}), ("search", {"query": "middleware", "fields": ["no_such_field"]})]

    plain = asyncio.run(_session_calls(_app(FastMCP), "probe", calls))
    counting = asyncio.run(_session_calls(_app(server.KnowledgeMCP), "probe", calls))
    server.drain_tool_call_counts(timeout=10)

    assert [r.model_dump() for r in counting] == [r.model_dump() for r in plain]
    assert counted.pending_tool_calls()[usage.utc_day()]["list_vaults"]["probe"]["stdio"]["calls"] == 1


class _BrokenTracker:
    def record_call(self, *_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")


def test_a_broken_tracker_never_fails_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.default_runtime(), "usage", _BrokenTracker())

    (result,) = asyncio.run(_session_calls(_app(server.KnowledgeMCP), "probe", [("list_vaults", {})]))
    server.drain_tool_call_counts(timeout=10)

    assert result.isError is False


def test_a_default_vault_not_open_yet_is_not_opened_to_count(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = server.registry()
    monkeypatch.setattr(reg, "default_if_open", lambda: None)
    monkeypatch.setattr(reg, "get", lambda *_a, **_k: pytest.fail("counting opened a vault"))

    server._count_call(server.mcp, "search", failed=False)
    server.drain_tool_call_counts(timeout=10)


_HOLD_LOCK = textwrap.dedent(
    """
    import fcntl, sys, time
    with open(sys.argv[1], "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        print("locked", flush=True)
        time.sleep(float(sys.argv[2]))
    """
)


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl locks are POSIX")
def test_a_call_does_not_wait_for_a_usage_lock_held_by_another_process(counted: UsageTracker) -> None:
    """Another process on the vault holds the usage file lock: every call still answers at once.

    Twenty-one calls force the tracker to flush once, and that flush waits on
    the lock. The calls answer while it waits; the counts land once the other
    process lets the lock go.
    """
    lock_path = counted._path.with_name(counted._path.name + ".lock")
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(lock_path), "6"], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        started = time.monotonic()
        results = asyncio.run(
            _session_calls(_app(server.KnowledgeMCP), "probe", [("list_vaults", {})] * 21)
        )
        elapsed = time.monotonic() - started
    finally:
        holder.wait(timeout=30)

    assert [r.isError for r in results] == [False] * 21
    assert elapsed < 4, f"the calls waited {elapsed:.1f}s on the usage file lock"
    server.drain_tool_call_counts(timeout=30)
    assert _on_disk(counted._path)[usage.utc_day()]["list_vaults"]["probe"]["stdio"]["calls"] >= 20


def test_the_module_server_is_the_counting_one() -> None:
    assert isinstance(server.mcp, server.KnowledgeMCP)


def test_closing_the_registry_writes_the_pending_calls(tmp_path: Path) -> None:
    from knowledge.registry import VaultRegistry

    loaded = settings.load_settings(
        {"VAULT_PATH": str(FIXTURE_VAULT), "KNOWLEDGE_INDEX_DIR": str(tmp_path / "index")},
        project_root=tmp_path,
    )
    reg = VaultRegistry.from_settings(loaded)
    assert reg.default_if_open() is None
    rt = reg.default
    assert reg.default_if_open() is rt
    rt.usage.record_call("search", client="c", principal="stdio", error=False)

    reg.close()

    assert _on_disk(tmp_path / "index" / "usage_stats.json")[usage.utc_day()]["search"]["c"]["stdio"]["calls"] == 1


# ---------------------------------------------------------------------------
# Over HTTP the principal is the bearer's id, never the bearer
# ---------------------------------------------------------------------------


@contextmanager
def _serving(app: FastMCP) -> Iterator[str]:
    asgi = server.http_app(app, serve_reindex_webhook=False)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    uv = uvicorn.Server(uvicorn.Config(asgi, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=uv.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not uv.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        uv.should_exit = True
        thread.join(timeout=15)
        sock.close()


async def _http_calls(base: str, calls: list[str]) -> list[CallToolResult]:
    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(60)) as http:
        async with streamable_http_client(f"{base}/mcp", http_client=http) as (read, write, _sid):
            async with ClientSession(read, write, client_info=Implementation(name="mcp-remote", version="0")) as s:
                await s.initialize()
                return [await s.call_tool(name, {}) for name in calls]


def test_over_http_the_principal_is_the_bearer_id(
    counted: UsageTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_API_KEY", API_TOKEN)
    settings.reset_current()

    with _serving(_app(server.KnowledgeMCP)) as base:
        results = asyncio.run(_http_calls(base, ["list_vaults", "no_such_tool"]))
    server.drain_tool_call_counts(timeout=10)
    settings.reset_current()

    assert [r.isError for r in results] == [False, True]
    day = counted.pending_tool_calls()[usage.utc_day()]
    assert day["list_vaults"] == {"mcp-remote": {principals.SERVICE_ID: {"calls": 1, "errors": 0}}}
    assert day["no_such_tool"] == {"mcp-remote": {principals.SERVICE_ID: {"calls": 1, "errors": 1}}}
    counted.flush()
    assert API_TOKEN not in counted._path.read_text()


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@pytest.fixture
def doctor_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(cli, "load_dotenv", lambda *_a, **_k: False)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    for key in ("KNOWLEDGE_VAULTS", "OPENAI_API_KEY", "CHROMA_PERSIST_DIR"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("KNOWLEDGE_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("KNOWLEDGE_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("KNOWLEDGE_EMBEDDINGS_PROVIDER", "bag-of-words")
    monkeypatch.setenv("VAULT_PATH", str(FIXTURE_VAULT))
    return tmp_path


def test_doctor_reports_the_tool_calls_of_the_default_vault(
    doctor_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tracker = UsageTracker(doctor_env / "index" / "usage_stats.json")
    tracker.record_call("search", client="claude-code", principal="stdio", error=False, latency_ms=14.0)
    tracker.record_call("search_code", client="claude-code", principal="stdio", error=False)
    tracker.flush()

    assert cli.main(["doctor"]) == 0
    block = json.loads(capsys.readouterr().out)["vaults"][0]["tool_calls"]

    assert block["days_kept"] == usage.TOOL_CALL_DAYS_KEPT
    assert block["last_7_days"]["by_tool"] == {"search": 1, "search_code": 1}
    assert block["last_7_days"]["by_client"] == {"claude-code": 2}
    assert block["last_7_days"]["by_principal"] == {"stdio": 2}
    assert block["deprecated"] == {"search_code": 1}
    assert block["unregistered"] == {}
    assert "search" in contract.TOOL_SINCE and "search_code" in contract.TOOL_REMOVED
    # Only the timed call has a histogram; an untimed one adds nothing to it.
    assert block["latency_ms"] == {"search": {"calls": 1, "p50": 20, "p95": 20}}


def test_doctor_without_calls_reports_an_empty_block_and_writes_nothing(
    doctor_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["doctor"]) == 0
    block = json.loads(capsys.readouterr().out)["vaults"][0]["tool_calls"]

    assert block["kept"]["calls"] == 0 and block["first_day"] is None
    assert "latency_ms" not in block  # no timed call: the block is the one 0.9.0 reported
    assert not (doctor_env / "index").exists()


def test_doctor_brief_does_not_grow(doctor_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["doctor", "--brief"]) == 0
    line = capsys.readouterr().out.strip()

    assert "tool_calls" not in line and len(line.encode()) <= cli.BRIEF_MAX_BYTES
