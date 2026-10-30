"""The chunker the index embeds with (``knowledge.chunker``).

The recall sweep ran with the ``sized_chunks`` of
``scripts/sweep_server.py``. The reference below is that
implementation, verbatim, with the header split it imported from the indexer:
the chunker the server ships must cut every body into the same bytes, or the
measurement does not describe it.
"""

from __future__ import annotations

import json
from pathlib import Path

import frontmatter
import pytest

from knowledge import chunker
from knowledge.chunker import EMPTY_BODY, ChunkPlan, chunks, plan_for, sized_chunks
from knowledge.embeddings import HARD_TRUNCATE_CHARS, BagOfWordsEmbeddings, NullEmbeddings, OpenAIEmbeddings
from knowledge.indexer import VaultIndexer
from knowledge.stores import InMemoryVectorStore
from knowledge.vault_source import DirectoryVaultSource

FIXTURE_VAULT = Path(__file__).resolve().parent / "fixtures" / "vault"


# --- the measured implementation, verbatim --------------------------------------


def _reference_split_by_headers(content: str) -> list[str]:
    sections: list[str] = []
    current: list[str] = []
    for line in content.splitlines(keepends=True):
        if line.startswith("## ") and current:
            sections.append("".join(current))
            current = []
        current.append(line)
    if current:
        sections.append("".join(current))
    return sections


def _reference_split_long(section: str, target: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for line in section.splitlines(keepends=True):
        while len(line) > target:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:target])
            line = line[target:]
        if current and len(current) + len(line) > target:
            pieces.append(current)
            current = ""
        current += line
    if current:
        pieces.append(current)
    return pieces


def _reference_sized_chunks(content: str, target: int, overlap: float) -> list[str]:
    if not content.strip():
        return ["(empty)"]
    pieces: list[str] = []
    for section in _reference_split_by_headers(content):
        pieces.extend([section] if len(section) <= target else _reference_split_long(section, target))

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > target:
            chunks.append(current)
            current = ""
        current += piece
    if current:
        chunks.append(current)

    tail = int(target * overlap)
    if tail <= 0 or len(chunks) < 2:
        return chunks
    return [chunks[0], *(chunks[i - 1][-tail:] + chunks[i] for i in range(1, len(chunks)))]


# --- bodies ------------------------------------------------------------------------


def _fixture_bodies() -> list[str]:
    return [frontmatter.load(str(p)).content for p in sorted(FIXTURE_VAULT.rglob("*.md"))]


def _synthetic_bodies() -> list[str]:
    section = "## Section {i}\n" + ("palabra con acento ñandú " * 40) + "\n"
    return [
        "",
        "   \n\t\n",
        "# Title only\n",
        "no headings at all\n" * 400,
        "# Title\nintro\n" + "".join(section.format(i=i) for i in range(30)),
        "## One huge line\n" + "x" * 9_000 + "\n## After\nshort\n",
        "```\n## not a heading inside a fence\n```\n" + "## Real\n" + "y " * 2_000,
        "".join(f"## S{i}\n" + "z" * (2_400 + i) + "\n" for i in range(5)),
        "line without newline at the end" * 200,
    ]


BODIES = _fixture_bodies() + _synthetic_bodies()
PLANS = [(2_500, 0.10), (1_000, 0.10), (400, 0.0), (500, 0.25), (100, 0.5)]


@pytest.mark.parametrize(("target", "overlap"), PLANS)
def test_the_chunker_cuts_exactly_like_the_measured_one(target: int, overlap: float) -> None:
    for body in BODIES:
        assert sized_chunks(body, target, overlap) == _reference_sized_chunks(body, target, overlap)


def test_the_header_split_is_the_measured_one() -> None:
    for body in BODIES:
        assert chunker.split_by_headers(body) == _reference_split_by_headers(body)


@pytest.mark.parametrize(("target", "overlap"), PLANS)
def test_each_chunk_knows_how_much_of_the_previous_one_it_repeats(target: int, overlap: float) -> None:
    plan = ChunkPlan(target=target, overlap=overlap)
    for body in BODIES:
        cut = chunks(body, plan)

        assert [c.text for c in cut] == sized_chunks(body, target, overlap)
        assert cut[0].overlap == 0
        assert all(0 <= c.overlap <= plan.tail for c in cut)
        for previous, current in zip(cut, cut[1:], strict=False):
            assert current.text[: current.overlap] == previous.own_text[len(previous.own_text) - current.overlap :]
        if body.strip():
            assert "".join(c.own_text for c in cut) == body


def test_a_blank_body_is_one_placeholder_chunk() -> None:
    assert [c.text for c in chunks("  \n", plan_for("none"))] == [EMPTY_BODY]


# --- plans and the provider guard ---------------------------------------------------


def test_every_provider_chunks_at_2500_characters_with_10_percent_overlap() -> None:
    for provider in ("openai", "none", "bag-of-words", "anything-else"):
        plan = plan_for(provider)
        assert (plan.target, plan.overlap, plan.tail, plan.largest_chunk) == (2_500, 0.10, 250, 2_750)
    assert plan_for("openai").version == "h2-pack-v1:2500:0.1"


def test_a_plan_that_could_outgrow_the_provider_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="truncates input over 7500"):
        ChunkPlan(target=7_000, overlap=0.10, max_chars=HARD_TRUNCATE_CHARS)
    with pytest.raises(ValueError):
        ChunkPlan(target=0, overlap=0.1)
    with pytest.raises(ValueError):
        ChunkPlan(target=100, overlap=1.0)


def _indexer(embedder, tmp_path: Path, vault: Path = FIXTURE_VAULT) -> VaultIndexer:
    return VaultIndexer(
        source=DirectoryVaultSource(vault), store=InMemoryVectorStore(), embedder=embedder, state_dir=tmp_path
    )


def test_the_openai_plan_is_guarded_by_the_provider_truncation(tmp_path: Path) -> None:
    indexer = _indexer(OpenAIEmbeddings(api_key="unused", model="text-embedding-3-large"), tmp_path)

    assert indexer.chunk_plan.max_chars == OpenAIEmbeddings.max_input_chars == HARD_TRUNCATE_CHARS
    longest = max(len(c.text) for body in BODIES for c in chunks(body, indexer.chunk_plan))
    assert longest <= indexer.chunk_plan.largest_chunk <= HARD_TRUNCATE_CHARS


def test_providers_without_an_input_limit_get_no_guard(tmp_path: Path) -> None:
    assert _indexer(NullEmbeddings(), tmp_path).chunk_plan.max_chars is None
    assert _indexer(BagOfWordsEmbeddings(), tmp_path).chunk_plan.max_chars is None


# --- what the indexer stores ---------------------------------------------------------


def _long_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "Docs").mkdir(parents=True)
    body = "# Long\n" + "".join(f"## Part {i}\n" + ("middleware gateway outage " * 50) + "\n" for i in range(8))
    (vault / "Docs" / "Long.md").write_text("---\ntype: guide\n---\n" + body)
    (vault / "Docs" / "Empty.md").write_text("---\ntype: guide\n---\n")
    return vault


def test_the_indexer_stores_each_chunk_with_its_overlap(tmp_path: Path) -> None:
    indexer = _indexer(NullEmbeddings(), tmp_path / "state", _long_vault(tmp_path))
    indexer.index()

    store = indexer.store
    rows = sorted((c.file_path, c.ord, c.overlap, c.text) for c in store.chunks(store.file_hashes()))
    long_rows = [row for row in rows if row[0] == "Docs/Long.md"]
    assert len(long_rows) > 1
    assert all(len(doc) <= 2_750 for *_, doc in rows)
    assert sorted(overlap for _, _, overlap, _ in long_rows)[-1] == 250
    assert [r[2] for r in rows if r[0] == "Docs/Empty.md"] == [0]


def test_the_keyword_index_holds_each_body_once(tmp_path: Path) -> None:
    """Term counts and the selector's idf do not see the repeated prefixes."""
    vault = _long_vault(tmp_path)
    indexer = _indexer(NullEmbeddings(), tmp_path / "state", vault)
    indexer.index()

    body = frontmatter.load(str(vault / "Docs" / "Long.md")).content
    assert indexer.keyword_index.docs == {"Docs/Empty.md": EMPTY_BODY.lower(), "Docs/Long.md": body.lower()}


def test_the_keyword_index_of_the_golden_vault_matches_the_one_chunk_per_file_index(tmp_path: Path) -> None:
    """Every fixture body is under 2,500 characters: one chunk, the same text the 1.1 index held."""
    indexer = _indexer(NullEmbeddings(), tmp_path)
    stats = indexer.index()

    assert stats["chunks"] == stats["files"] == 14
    for path in sorted(FIXTURE_VAULT.rglob("*.md")):
        rel = path.relative_to(FIXTURE_VAULT).as_posix()
        body = frontmatter.load(str(path)).content
        assert indexer.keyword_index.docs[rel].strip() == (body if body.strip() else EMPTY_BODY).lower().strip()


def test_an_index_cut_by_another_plan_is_re_chunked_in_full(tmp_path: Path) -> None:
    indexer = _indexer(NullEmbeddings(), tmp_path)
    indexer.index()
    meta_path = tmp_path / "index_meta.json"
    assert json.loads(meta_path.read_text())["chunker"] == indexer.chunk_plan.version
    assert indexer.store.get_meta()["chunker_version"] == indexer.chunk_plan.version

    assert indexer.index()["unchanged"] == 14
    indexer.store.set_meta({"chunker_version": "h2-pack-v1:1000:0.1"})

    again = indexer.index()
    assert (again["updated"], again["unchanged"]) == (14, 0)
    assert indexer.index()["unchanged"] == 14


def test_an_excerpt_never_starts_inside_the_previous_chunk() -> None:
    from knowledge.ports import ChunkHit, ChunkRecord

    assert ChunkHit("a.md", 1, "tail of previous|own text", 17, 0.5).own_text == "own text"
    assert ChunkRecord("a.md", 0, "whole").own_text == "whole"
