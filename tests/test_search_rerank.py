"""``search(rerank=...)`` end to end: server, settings, the ``rerank`` extension and a loopback judge.

Every test serves a copy of the fixture vault whose ``vault.yaml`` declares a
``kev`` judge at the System One double of ``tests/systemone_fakes.py`` on
``127.0.0.1``, so the real adapter (``knowledge.judges``) speaks real HTTP and
nothing leaves the machine. What the double answers is chosen by a marker in
the query (``[529]``, ``[slow]``).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from knowledge import cli, errors, rerank, settings
from knowledge.errors import KnowledgeToolError
from knowledge.ports import NullJudge

from systemone_fakes import API_KEY, FakeSystemOne, systemone_server

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"
QUERY = "middleware outage"

#: A ``kev`` judge at the double; ``{extra}`` adds keys to the block.
BLOCK = (
    "rerank:\n"
    "  provider: kev\n"
    "  model: kev-latest\n"
    "  key_env: KEV_API_KEY\n"
    "  default: {default}\n"
    "  candidates: 5\n"
    "  base_url: {base_url}\n"
    "{extra}"
)

#: A document whose second block answers the query while the first only repeats its terms.
TWO_BLOCKS = (
    "---\ntitle: Lantern Guide\ntype: guide\n---\n\n"
    "Intro.\n\n"
    "## Overview\n\n"
    + "quokka zebra lantern " * 12
    + "ferry anchor bridge tunnel harbor beacon signal tower relay\n\n"
    "## Answer\n\n"
    "quokka zebra lantern\n"
)
TWO_BLOCKS_PATH = "Guides/Lantern Guide.md"


@dataclass
class Served:
    vault: Path
    index: Path
    origin: str
    fake: FakeSystemOne

    def serve(self, *, default: str = "off", extra: str = "", block: str | None = None, **env: str) -> settings.Settings:
        from knowledge import server

        manifest = BLOCK.format(default=default, base_url=self.origin, extra=extra) if block is None else block
        (self.vault / "vault.yaml").write_text(manifest, encoding="utf-8")
        base = {
            "VAULT_PATH": str(self.vault),
            "KNOWLEDGE_INDEX_DIR": str(self.index),
            "KNOWLEDGE_EMBEDDINGS_PROVIDER": "bag-of-words",
            "KEV_API_KEY": API_KEY,
        }
        base.update(env)
        loaded = settings.load_settings(base, project_root=self.vault.parent)
        settings.install(loaded)
        server.set_registry(server._registry_for(loaded))
        return loaded

    @property
    def usage_file(self) -> Path:
        return self.index / "usage_stats.json"


@pytest.fixture()
def served(tmp_path: Path) -> Iterator[Served]:
    from knowledge import server

    vault = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    try:
        with systemone_server(FakeSystemOne(drip_s=0.05)) as (origin, fake):
            yield Served(vault=vault, index=tmp_path / "index", origin=origin, fake=fake)
    finally:
        server.set_registry(None)


def _search(**kwargs: Any) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """``search`` as the server runs it, with the ``_meta`` its result would carry."""
    from knowledge import server

    sink: dict[str, Any] = {}
    token = server._tool_meta.set(sink)
    try:
        rows = asyncio.run(server.search(**kwargs))
    finally:
        server._tool_meta.reset(token)
    return rows, sink.get(server.RERANK_META_KEY)


def _row(rows: list[dict[str, Any]], file_path: str) -> dict[str, Any]:
    return next(row for row in rows if row["file_path"] == file_path)


# --- the contract ------------------------------------------------------------------


def test_without_rerank_the_search_is_the_plain_one_and_carries_no_meta(served: Served) -> None:
    served.serve()

    plain, meta = _search(query=QUERY, limit=5)
    off, off_meta = _search(query=QUERY, limit=5, rerank="off")

    assert meta is None
    assert all("rerank_score" not in row for row in plain)
    assert [row["file_path"] for row in off] == [row["file_path"] for row in plain]
    assert off_meta is not None and (off_meta["applied"], off_meta["reason"]) == (False, "off")
    assert served.fake.bodies == []


def test_rerank_on_reorders_with_the_judge_and_reports_it(served: Served) -> None:
    served.serve()

    rows, meta = _search(query=QUERY, limit=5, rerank="on")

    assert meta == {
        "provider": "kev",
        "model": "kev-latest",
        "applied": True,
        "reason": None,
        "candidates": 5,
        "units": meta["units"],
        "cache_hits": 0,
    }
    assert len(served.fake.bodies) == 1
    sent = served.fake.bodies[0]
    assert sent["model"] == "kev-latest" and len(sent["state"]["candidates"]) == meta["units"]
    assert any("rerank_score" in row for row in rows)
    assert [row["score"] for row in rows] == sorted((row["score"] for row in rows), reverse=True)


def test_the_vault_default_turns_rerank_on_and_off_overrides_it(served: Served) -> None:
    served.serve(default="on")

    _, meta = _search(query=QUERY)
    _, off = _search(query=QUERY, rerank="off")

    assert meta is not None and meta["applied"] is True
    assert off is not None and off["reason"] == "off"


@pytest.mark.parametrize("value", ["yes", "ON", "", "jev"])
def test_an_unknown_rerank_value_is_refused_before_anything_runs(served: Served, value: str) -> None:
    served.serve()

    with pytest.raises(KnowledgeToolError) as refused:
        _search(query=QUERY, rerank=value)

    assert refused.value.code == errors.E_ARGUMENT_OUT_OF_RANGE
    assert not served.usage_file.exists() and served.fake.bodies == []


def test_rerank_on_without_a_judge_is_refused_before_anything_runs(served: Served) -> None:
    served.serve(block="")

    with pytest.raises(KnowledgeToolError) as refused:
        _search(query=QUERY, rerank="on")

    assert refused.value.code == errors.E_RERANK_NOT_CONFIGURED
    assert "declares no rerank.provider" in refused.value.message
    assert not served.usage_file.exists()


def test_the_pointers_name_the_block_the_judge_chose(served: Served) -> None:
    from knowledge import server

    (served.vault / TWO_BLOCKS_PATH).parent.mkdir(parents=True, exist_ok=True)
    (served.vault / TWO_BLOCKS_PATH).write_text(TWO_BLOCKS, encoding="utf-8")
    served.serve(extra="  units_per_doc: 3\n")
    fields = ["file_path", "chunk_id", "section", "rerank_score"]

    plain = _row(_search(query="quokka zebra lantern", fields=fields)[0], TWO_BLOCKS_PATH)
    rows, meta = _search(query="quokka zebra lantern", fields=fields, rerank="on")
    judged = _row(rows, TWO_BLOCKS_PATH)

    assert meta is not None and meta["applied"] is True
    assert plain["section"] == "Overview" and "rerank_score" not in plain
    assert judged["section"] == "Answer" and 0 < judged["rerank_score"] <= 1
    read = asyncio.run(server.get_document(TWO_BLOCKS_PATH, chunk_id=judged["chunk_id"], max_chars=2000))
    assert read["section"] == "Answer" and read["content"].startswith("## Answer")


def test_rerank_score_in_fields_is_absent_when_the_judge_did_not_run(served: Served) -> None:
    served.serve()

    rows, _ = _search(query=QUERY, fields=["file_path", "rerank_score"], rerank="off")

    assert rows and all(set(row) == {"file_path"} for row in rows)


# --- failures never fail the search ---------------------------------------------


def test_a_judge_that_answers_529_leaves_the_plain_ranking(served: Served) -> None:
    served.serve()
    query = "otp session [529]"

    plain, _ = _search(query=query, limit=3, rerank="off")
    down, meta = _search(query=query, limit=3, rerank="on")

    assert meta is not None and (meta["applied"], meta["reason"]) == (False, "unavailable")
    assert [row["file_path"] for row in down] == [row["file_path"] for row in plain]
    assert all("rerank_score" not in row for row in down)


def test_a_judge_that_trickles_returns_on_time_with_timeout(served: Served) -> None:
    served.serve(extra="  timeout_ms: 300\n")
    _search(query="otp session", limit=3)  # index the vault outside the clock

    started = time.monotonic()
    rows, meta = _search(query="otp session [slow]", limit=3, rerank="on")
    elapsed = time.monotonic() - started

    assert meta is not None and (meta["applied"], meta["reason"]) == (False, "timeout")
    # The whole search, retrieval included, ends within the judge's deadline plus 100 ms.
    assert rows and elapsed <= 0.3 + 0.1, elapsed


def test_a_missing_key_with_default_on_starts_and_searches_without_a_judge(served: Served) -> None:
    """The deployment that declares ``jev`` with ``default: on`` before its key is loaded."""
    from knowledge import server

    block = (
        "rerank:\n  provider: jev\n  model: jev-1.13.0\n  key_env: TYPESAFE_API_KEY\n  default: on\n"
    )
    loaded = served.serve(block=block, TYPESAFE_API_KEY="")
    warnings = loaded.validate()

    assert any(w.startswith(f"{errors.W_RERANK_KEY_MISSING}: ") and "TYPESAFE_API_KEY" in w for w in warnings)
    assert isinstance(server.registry().default.judge, NullJudge)
    rows, meta = _search(query=QUERY)
    assert rows and meta is not None and (meta["applied"], meta["reason"]) == (False, "key_missing")
    assert (meta["provider"], meta["model"]) == ("jev", "jev-1.13.0")
    assert served.fake.bodies == []


def test_the_operator_switch_turns_every_judge_off(served: Served) -> None:
    served.serve(default="on", KNOWLEDGE_RERANK="off")

    _, meta = _search(query=QUERY)

    assert meta is not None and meta["reason"] == "disabled" and served.fake.bodies == []


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [({"mode": "semantic"}, "mode"), ({"mode": "keyword"}, "mode"), ({"include_memories": True}, "memories_included")],
)
def test_only_a_hybrid_search_without_memories_reaches_the_judge(
    served: Served, kwargs: dict[str, Any], reason: str
) -> None:
    served.serve()

    _, meta = _search(query=QUERY, rerank="on", **kwargs)

    assert meta is not None and meta["reason"] == reason and served.fake.bodies == []


def test_never_send_keeps_a_folder_out_of_every_request(served: Served) -> None:
    served.serve(extra="  never_send: [Runbooks/]\n")

    rows, meta = _search(query=QUERY, rerank="on")

    assert meta is not None and meta["applied"] is True
    documents = {c["document"] for body in served.fake.bodies for c in body["state"]["candidates"]}
    assert documents and "Runbook - Middleware Outage" not in documents
    runbook = next(row for row in rows if row["file_path"].startswith("Runbooks/"))
    assert "rerank_score" not in runbook


def test_the_memory_folder_never_reaches_the_judge(served: Served) -> None:
    memory = served.vault / "90 - Memory" / "Middleware Outage Memory.md"
    memory.parent.mkdir(parents=True)
    memory.write_text(
        "---\ntitle: Middleware Outage Memory\ntype: memory\n---\n\n# Memory\n\nmiddleware outage middleware outage\n",
        encoding="utf-8",
    )
    served.serve()

    _search(query=QUERY, rerank="on", limit=10)

    sent = json.dumps(served.fake.bodies, ensure_ascii=False)
    assert served.fake.bodies and "Middleware Outage Memory" not in sent and "type: memory" not in sent


# --- usage ---------------------------------------------------------------------------


def test_only_the_final_rows_are_counted_as_used(served: Served) -> None:
    from knowledge import server

    served.serve()
    rows, meta = _search(query=QUERY, limit=3, rerank="on")
    assert meta is not None and meta["applied"] is True
    usage = server.registry().default.usage

    used = {fp for fp in (row["file_path"] for row in rows) if usage.uses(fp) > 0}
    assert used == {row["file_path"] for row in rows}
    assert sum(1 for fp in usage._files if usage.uses(fp) > 0) == 3


def test_the_rerank_counters_and_the_daily_searches_reach_the_usage_file(served: Served) -> None:
    from knowledge import server

    served.serve()
    _search(query=QUERY, rerank="on")
    _search(query=QUERY, rerank="on")
    _search(query=QUERY)
    server.registry().default.usage.flush()

    raw = json.loads(served.usage_file.read_text())["rerank"]
    assert raw["calls"] == 2 and raw["applied"] == 2 and raw["cache_hits"] == raw["units"] // 2
    assert sum(day["searches"] for day in raw["searches_by_day"].values()) == 3
    assert QUERY not in json.dumps(raw)


def test_doctor_reports_the_judge_by_name_never_by_value(served: Served) -> None:
    loaded = served.serve(default="on")

    report = cli._doctor_report({"KEV_API_KEY": API_KEY}, loaded)
    block = report["vaults"][0]["rerank"]

    assert {k: block[k] for k in ("provider", "default", "model", "key", "allowed")} == {
        "provider": "kev",
        "default": "on",
        "model": "kev-latest",
        "key": "ok",
        "allowed": True,
    }
    assert API_KEY not in json.dumps(report)
    assert "rerank" in cli._brief_line(report) and "kev/on" in cli._brief_line(report)


def test_doctor_names_a_missing_key(served: Served) -> None:
    loaded = served.serve(KEV_API_KEY="")

    block = cli._doctor_report({}, loaded)["vaults"][0]["rerank"]

    assert block["key"] == "missing:KEV_API_KEY"


def test_the_server_imports_the_judge_only_for_a_vault_that_declares_one(served: Served) -> None:
    from knowledge import server

    served.serve(block="")
    assert isinstance(server.registry().default.judge, NullJudge)
    served.serve()
    assert server.registry().default.judge.name == "kev"
    assert rerank.pool_limit(5) == 20
