"""The hyperparameter sweep harness (``scripts/sweep_hyperparams.py``).

The experimental chunker and RRF are pinned with hand-built inputs. The smoke
case runs one real configuration end to end over the fixture vault: the sweep
launcher installs the chunker and hands over to the golden stdio launcher,
which embeds with the bag-of-words double, so no key, no network and no real
vault are involved. It proves the plumbing, never the numbers.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).parent
PROJECT_ROOT = TESTS_DIR.parent
SCRIPTS = PROJECT_ROOT / "scripts"
FIXTURE_VAULT = TESTS_DIR / "fixtures" / "vault"
FIXTURE_QUERIES = PROJECT_ROOT / "tests" / "fixtures" / "eval" / "queries.yaml"
GOLDEN_SERVER = TESTS_DIR / "golden_stdio_server.py"


def _load(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sweep = _load("sweep_hyperparams")
launcher = _load("sweep_server")


def test_rrf_rewards_documents_both_lists_agree_on() -> None:
    # b: 1/62 + 1/61; a: 1/61; d: 1/62; c: 1/63.
    assert sweep.rrf_fuse([["a", "b", "c"], ["b", "d"]], 60, 3) == ["b", "a", "d"]


def test_rrf_k_controls_how_much_the_top_rank_dominates() -> None:
    # p is first in one list only; q is second in one and sixth in the other.
    lists = [["p", "q"], ["r", "s", "t", "u", "v", "q"]]

    assert sweep.rrf_fuse(lists, 1, 1) == ["p"]  # 1/2 beats 1/3 + 1/7
    assert sweep.rrf_fuse(lists, 100, 1) == ["q"]  # 1/102 + 1/106 beats 1/101


def test_sized_chunks_respect_the_target_and_overlap() -> None:
    body = "".join(f"## Section {i}\n" + ("word " * 60) + "\n" for i in range(6))

    chunks = launcher.sized_chunks(body, 500, 0.1)

    assert len(chunks) > 1
    tail = int(500 * 0.1)
    assert all(len(c) <= 500 + tail for c in chunks)
    for previous, current in zip(chunks, chunks[1:]):
        assert current.startswith(previous[-tail:])


def test_sized_chunks_without_overlap_reassemble_the_document() -> None:
    body = "# Title\nintro\n" + "".join(f"## S{i}\n" + "x" * 300 + "\n" for i in range(4)) + "y" * 1200 + "\n"

    chunks = launcher.sized_chunks(body, 400, 0.0)

    assert "".join(chunks) == body
    assert all(len(c) <= 400 for c in chunks)


def test_blank_documents_keep_the_server_placeholder() -> None:
    assert launcher.sized_chunks("  \n", 1000, 0.1) == ["(empty)"]


def test_one_configuration_end_to_end_over_the_fixture(tmp_path: Path) -> None:
    import eval_retrieval as ev

    qset = ev.load_query_set(FIXTURE_QUERIES)

    def run(config):
        return asyncio.run(
            sweep.sweep_config(
                config,
                qset,
                lambda text: len(text.split()),
                vault=FIXTURE_VAULT,
                rrf_ks=(20, 60),
                depth=10,
                workdir=tmp_path,
                inner=GOLDEN_SERVER,
                extra_env={"OPENAI_API_KEY": "placeholder-not-a-credential"},
            )
        )

    baseline = run(sweep.ChunkConfig(0, 0.0))
    small = run(sweep.ChunkConfig(120, 0.1))

    assert [r.fusion for r in baseline] == ["híbrida actual (0,7·sem + 0,3·kw)", "RRF k=20", "RRF k=60"]
    assert baseline[0].chunks is not None and small[0].chunks is not None
    assert small[0].chunks > baseline[0].chunks
    assert all(r.recall > 0 for r in baseline + small)
    assert "RRF k=60" in sweep.render_markdown(baseline + small, qset.k)
