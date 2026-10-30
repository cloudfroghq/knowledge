"""Cut a Markdown body into the chunks the index embeds.

``sized_chunks`` packs level-2 sections (lines starting with ``## ``) into
chunks of at most ``target`` characters, cuts a longer section at line
boundaries (a single longer line is cut hard), and starts every chunk after
the first with the last ``int(target * overlap)`` characters of the previous
one, so a fact split across a boundary is still embedded whole once. It is the
chunker ``scripts/sweep_server.py`` measured, byte for byte; ``tests/test_chunker.py``
holds the measured implementation as its reference.

A leaf of the core: it imports nothing from the package. The provider's input
limit reaches it as a number through :func:`plan_for`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

#: Name of the packing rules; :attr:`ChunkPlan.version` adds the plan's numbers.
CHUNKER_VERSION = "h2-pack-v1"

#: The one chunk of a body with no text.
EMPTY_BODY = "(empty)"


@dataclass(frozen=True)
class ChunkPlan:
    """How large chunks are and how much of the previous one each repeats.

    ``max_chars`` is the provider's input limit in characters, or ``None`` for a
    provider without one. A plan whose largest possible chunk (``target`` plus
    its overlap) exceeds it is refused when it is built, so no chunk ever
    reaches the provider's silent truncation.
    """

    target: int
    overlap: float
    max_chars: int | None = None

    def __post_init__(self) -> None:
        if self.target <= 0 or not 0 <= self.overlap < 1:
            raise ValueError(f"invalid chunk plan: target={self.target}, overlap={self.overlap}")
        if self.max_chars is not None and self.largest_chunk > self.max_chars:
            raise ValueError(
                f"chunk plan {self.version} can produce {self.largest_chunk}-character chunks; "
                f"the provider truncates input over {self.max_chars}"
            )

    @property
    def tail(self) -> int:
        """Characters each chunk after the first repeats from the previous one."""
        return int(self.target * self.overlap)

    @property
    def largest_chunk(self) -> int:
        """Upper bound of a chunk's length under this plan."""
        return self.target + self.tail

    @property
    def version(self) -> str:
        """``<rules>:<target>:<overlap>``, recorded with the index it built."""
        return f"{CHUNKER_VERSION}:{self.target}:{self.overlap}"


@dataclass(frozen=True)
class Chunk:
    """One chunk: the text that is embedded and how many leading characters repeat the previous chunk."""

    text: str
    overlap: int

    @property
    def own_text(self) -> str:
        """The chunk without the characters it repeats from the previous one."""
        return self.text[self.overlap :]


#: Target size and overlap per embeddings provider; any other provider gets
#: :data:`DEFAULT_PLAN`'s numbers. Every provider chunks at 2,500 characters
#: with 10 % overlap, the configuration the hyperparameter sweep
#: (``scripts/sweep_hyperparams.py``) selected.
_PLAN_NUMBERS: Mapping[str, tuple[int, float]] = MappingProxyType(
    {
        "openai": (2_500, 0.10),
        "none": (2_500, 0.10),
        "bag-of-words": (2_500, 0.10),
    }
)

DEFAULT_PLAN = ChunkPlan(target=2_500, overlap=0.10)


def plan_for(provider: str, *, max_input_chars: int | None = None) -> ChunkPlan:
    """The chunk plan of ``provider``, guarded by its input limit in characters."""
    target, overlap = _PLAN_NUMBERS.get(provider, (DEFAULT_PLAN.target, DEFAULT_PLAN.overlap))
    return ChunkPlan(target=target, overlap=overlap, max_chars=max_input_chars)


def split_by_headers(content: str) -> list[str]:
    """Split markdown content by ## headers, keeping each section together."""
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


def _split_long(section: str, target: int) -> list[str]:
    """Cut a section longer than ``target`` at line boundaries (hard cut for one huge line)."""
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


def sized_chunks(content: str, target: int, overlap: float) -> list[str]:
    """Pack ``##`` sections into chunks of at most ``target`` chars, with overlap.

    Sections are kept whole when they fit and packed greedily with their
    neighbours; a section longer than ``target`` is cut at line boundaries.
    Each chunk after the first starts with the last ``overlap * target``
    characters of the previous one, so a fact split across a boundary is
    still embedded whole once.
    """
    if not content.strip():
        return [EMPTY_BODY]
    pieces: list[str] = []
    for section in split_by_headers(content):
        pieces.extend([section] if len(section) <= target else _split_long(section, target))

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


def chunks(body: str, plan: ChunkPlan) -> list[Chunk]:
    """``body`` cut under ``plan``, each chunk with the length of its repeated prefix.

    The texts are exactly :func:`sized_chunks`; ``overlap`` is how many leading
    characters of a chunk come from the previous one (0 for the first).
    """
    texts = sized_chunks(body, plan.target, plan.overlap)
    out: list[Chunk] = []
    previous_own = ""
    for index, text in enumerate(texts):
        repeated = min(plan.tail, len(previous_own)) if index and plan.tail > 0 else 0
        out.append(Chunk(text=text, overlap=repeated))
        previous_own = text[repeated:]
    return out
