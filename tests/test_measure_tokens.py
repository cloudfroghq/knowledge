"""The token meter (``scripts/measure_tokens.py``) over the golden corpus.

The corpus is ``tests/golden/v<contract version>/``, so the meter is checked against the same
bytes the contract test pins. Tokens are counted with a whitespace counter:
the real tokenizer downloads its vocabulary once and the suite stays offline.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("measure_tokens", PROJECT_ROOT / "scripts" / "measure_tokens.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["measure_tokens"] = module
    spec.loader.exec_module(module)
    return module


mt = _load()


def _words(text: str) -> int:
    return len(text.split())


def test_every_golden_is_measured_and_the_catalog_apart() -> None:
    goldens = mt.load_goldens()

    catalog, responses = mt.measure_all(goldens, _words)

    assert catalog.name == "tools_list"
    assert {m.name for m in responses} == set(goldens) - {"tools_list", "initialize"}
    assert catalog.wire_bytes == len(mt._compact(goldens["tools_list"]).encode("utf-8"))


def test_content_is_the_text_blocks_and_wire_is_the_whole_result() -> None:
    payload = {
        "content": [{"type": "text", "text": "one two"}, {"type": "text", "text": " three"}],
        "isError": False,
        "structuredContent": {"result": ["x"]},
    }

    m = mt.measure_response("demo", payload, _words)

    assert (m.content_chars, m.content_tokens) == (len("one two three"), 3)
    assert m.wire_bytes == len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    assert m.wire_tokens >= m.content_tokens


def test_flows_add_the_responses_they_chain() -> None:
    catalog, responses = mt.measure_all(mt.load_goldens(), _words)
    by_name = {m.name: m for m in responses}

    rendered = mt.render_markdown(catalog, responses, "words")

    expected = by_name["search"].content_tokens + by_name["get_document"].content_tokens
    assert f"| search → get_document | 2 | {expected} |" in rendered
    assert "Arranque de sesión" in rendered
    for name in by_name:
        assert f"`{name}`" in rendered


def test_real_reads_render_the_flow_when_search_tokens_are_given() -> None:
    reads = mt.RealReads(tokens=[100, 200, 300], truncated=2, search_tokens=[10, 20, 30])

    rendered = mt.render_real_reads(reads, "demo")

    assert "Flujo `search → get_document` | 3 | 220 · 220 · 330 |" in rendered
    assert "2 de 3 lecturas" in rendered


def test_flow_table_reports_tokens_truncation_and_coverage() -> None:
    rows = [
        mt.FlowRow("a", search_tokens=10, read_tokens=100, truncated=True, covered=True),
        mt.FlowRow("b", search_tokens=20, read_tokens=200, truncated=False, covered=False),
        mt.FlowRow("c", search_tokens=30, read_tokens=300, truncated=True, covered=None),
    ]

    rendered = mt.render_flow(rows, "demo")

    assert "| `search` (`limit=5`) | 3 | 20 · 20 · 30 |" in rendered
    assert "| Flujo `search → get_document` | 3 | 220 · 220 · 330 |" in rendered
    assert "Lecturas truncadas: 2 de 3." in rendered
    assert "Cobertura (lecturas con todos los `answer_terms`): 1 de 2 (0.500)." in rendered


def test_the_corpus_is_the_golden_of_the_declared_contract_version() -> None:
    from knowledge import contract

    assert mt.GOLDEN_DIR.name == f"v{contract.CONTRACT_VERSION}"
    assert mt.GOLDEN_DIR.is_dir()


def test_baseline_flow_runs_against_a_local_server(tmp_path: Path, monkeypatch) -> None:
    """The flow drives a real stdio server; here the golden launcher stands in for the real one."""
    import asyncio

    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "scripts"))
    import eval_retrieval

    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-not-a-credential")
    fixture = PROJECT_ROOT / "tests" / "fixtures" / "vault"
    queries = PROJECT_ROOT / "tests" / "fixtures" / "eval" / "queries.yaml"
    original = eval_retrieval.stdio_session

    def with_golden_launcher(vault, **kwargs):
        kwargs["server_script"] = PROJECT_ROOT / "tests" / "golden_stdio_server.py"
        return original(vault, **kwargs)

    monkeypatch.setattr(eval_retrieval, "stdio_session", with_golden_launcher)
    with open(tmp_path / "log", "w", encoding="utf-8") as errlog:
        rows = asyncio.run(mt.run_baseline_flow(fixture, queries, _words, errlog, index_dir=tmp_path / "chroma"))

    assert len(rows) == len(eval_retrieval.load_query_set(queries).queries)
    assert all(r.search_tokens > 0 and r.read_tokens > 0 for r in rows)


def test_the_sections_flow_is_measured_on_the_goldens() -> None:
    catalog, responses = mt.measure_all(mt.load_goldens(), _words)
    by_name = {m.name: m for m in responses}

    rendered = mt.render_markdown(catalog, responses, "words")

    expected = by_name["search__pointers"].content_tokens + by_name["get_document__chunk"].content_tokens
    assert f"| search(fields) → get_document(chunk_id) | 2 | {expected} |" in rendered


def test_the_reader_picks_the_expected_row_or_the_first() -> None:
    rows = [{"file_path": "a.md", "chunk_id": "x:1"}, {"file_path": "b.md", "chunk_id": "y:2"}]

    assert mt.pick_row(rows, "b.md") == rows[1]
    assert mt.pick_row(rows, "c.md") == rows[0]
    assert mt.pick_row([], "c.md") is None


def test_sections_flow_runs_against_a_local_server(tmp_path: Path, monkeypatch) -> None:
    """The protocol of the ``instructions``: pointer fields, then one section, here on the fixture."""
    import asyncio

    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "scripts"))
    import eval_retrieval

    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-not-a-credential")
    fixture = PROJECT_ROOT / "tests" / "fixtures" / "vault"
    queries = PROJECT_ROOT / "tests" / "fixtures" / "eval" / "queries.yaml"
    original = eval_retrieval.stdio_session

    def with_golden_launcher(vault, **kwargs):
        kwargs["server_script"] = PROJECT_ROOT / "tests" / "golden_stdio_server.py"
        return original(vault, **kwargs)

    monkeypatch.setattr(eval_retrieval, "stdio_session", with_golden_launcher)
    with open(tmp_path / "log", "w", encoding="utf-8") as errlog:
        rows = asyncio.run(mt.run_sections_flow(fixture, queries, _words, errlog, index_dir=tmp_path / "chroma"))

    assert len(rows) == len(eval_retrieval.load_query_set(queries).queries)
    assert all(r.search_tokens > 0 and r.read_tokens > 0 for r in rows)
    rendered = mt.render_flow(rows, "demo", "sections")
    assert "`max_chars=2000`" in rendered
    assert "Lecturas de otro documento que el principal esperado" in rendered


def test_the_sections_flow_with_rerank_sends_it_and_reports_the_judge(tmp_path: Path, monkeypatch) -> None:
    """``--rerank on`` against the System One double: every search of the flow is judged."""
    import asyncio
    import shutil

    from systemone_fakes import API_KEY, FakeSystemOne, systemone_server

    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "scripts"))
    import eval_retrieval

    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-not-a-credential")
    vault = tmp_path / "vault"
    shutil.copytree(PROJECT_ROOT / "tests" / "fixtures" / "vault", vault)
    queries = PROJECT_ROOT / "tests" / "fixtures" / "eval" / "queries.yaml"
    original = eval_retrieval.stdio_session

    def with_golden_launcher(vault, **kwargs):
        kwargs["server_script"] = PROJECT_ROOT / "tests" / "golden_stdio_server.py"
        return original(vault, **kwargs)

    monkeypatch.setattr(eval_retrieval, "stdio_session", with_golden_launcher)
    with systemone_server(FakeSystemOne()) as (origin, _state):
        (vault / "vault.yaml").write_text(
            f"rerank:\n  provider: kev\n  model: kev-latest\n  key_env: KEV_API_KEY\n  base_url: {origin}\n",
            encoding="utf-8",
        )
        with open(tmp_path / "log", "w", encoding="utf-8") as errlog:
            rows = asyncio.run(
                mt.run_sections_flow(
                    vault,
                    queries,
                    _words,
                    errlog,
                    index_dir=tmp_path / "index",
                    extra_env={"KNOWLEDGE_GOLDEN_KEV_API_KEY": API_KEY},
                    rerank="on",
                )
            )

    assert rows and all(r.rerank_applied is True for r in rows)
    assert f"reordenadas por el juez: {len(rows)} de {len(rows)}" in mt.render_flow(rows, "demo", "sections")


def test_the_flow_search_omits_rerank_unless_asked() -> None:
    assert mt._search_arguments("q", 5, None) == {"query": "q", "limit": 5}
    assert mt._search_arguments("q", 5, "off", fields=["file_path"]) == {
        "query": "q",
        "limit": 5,
        "fields": ["file_path"],
        "rerank": "off",
    }


def test_the_defaults_flow_follows_the_instructions_protocol() -> None:
    """The measured protocol is the one the handshake tells a model to follow: no fields, no max_chars."""
    from knowledge import contract

    assert "search(query) -> get_document(chunk_id=<chunk_id>)" in contract.INSTRUCTIONS
    assert "fields=" not in contract.INSTRUCTIONS and "max_chars" not in contract.INSTRUCTIONS
    assert set(mt.FLOW_RUNNERS) == set(mt.FLOW_CALLS) == {"baseline", "sections", "defaults"}


def test_defaults_flow_runs_against_a_local_server(tmp_path: Path, monkeypatch) -> None:
    """``search(query)`` then ``get_document(chunk_id)`` with every default, on the fixture."""
    import asyncio

    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "scripts"))
    import eval_retrieval

    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-not-a-credential")
    fixture = PROJECT_ROOT / "tests" / "fixtures" / "vault"
    queries = PROJECT_ROOT / "tests" / "fixtures" / "eval" / "queries.yaml"
    original = eval_retrieval.stdio_session
    calls: list[tuple[str, dict]] = []

    def with_golden_launcher(vault, **kwargs):
        kwargs["server_script"] = PROJECT_ROOT / "tests" / "golden_stdio_server.py"
        return _spying(original(vault, **kwargs), calls)

    monkeypatch.setattr(eval_retrieval, "stdio_session", with_golden_launcher)
    with open(tmp_path / "log", "w", encoding="utf-8") as errlog:
        rows = asyncio.run(mt.run_defaults_flow(fixture, queries, _words, errlog, index_dir=tmp_path / "chroma"))

    assert len(rows) == len(eval_retrieval.load_query_set(queries).queries)
    assert all(r.search_tokens > 0 and r.read_tokens > 0 for r in rows)
    assert {name for name, _ in calls} == {"search", "get_document"}
    assert all(set(args) == {"query", "limit"} for name, args in calls if name == "search")
    assert all("max_chars" not in args and "fields" not in args for _, args in calls)
    assert any(set(args) == {"chunk_id"} for name, args in calls if name == "get_document")
    assert "sin `max_chars`" in mt.render_flow(rows, "demo", "defaults")


def _spying(session, calls: list):
    """Wrap a ``stdio_session`` context so every ``call(name, arguments)`` is recorded first."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def wrapped():
        async with session as call:
            async def spy(name, arguments):
                calls.append((name, dict(arguments)))
                return await call(name, arguments)

            yield spy

    return wrapped()


def test_the_protocol_reads_the_window_the_selector_scores() -> None:
    """The selector ranks blocks by what a reader of ``max_chars`` characters gets."""
    from knowledge import pointers

    assert mt.SECTION_MAX_CHARS == pointers.READ_WINDOW_CHARS


def test_the_catalog_is_measured_with_structured_output_on_and_off() -> None:
    import asyncio

    sizes = [mt.measure_handshake(s, *asyncio.run(mt.fetch_handshake(s)), _words) for s in (True, False)]

    on, off = sizes
    assert on.catalog_bytes > off.catalog_bytes
    assert on.instructions_chars == off.instructions_chars > 0
    rendered = mt.render_catalog(sizes, "words")
    assert "| `1` |" in rendered and "| `0` |" in rendered
