"""Stable pointers into a vault document: ``doc_id``, ``chunk_id`` and the section a query hits.

A search row can point at the part of a document that answers the query, so a
client reads one section instead of paging a whole file. The pointers are
computed from the path and the Markdown body alone; nothing here reads or
changes the index.

* ``doc_id`` is the first :data:`DOC_ID_HEX` hex digits of ``sha1(file_path)``.
  It is stable across processes and index rebuilds, and scoped to one vault.
* ``chunk_id`` is ``"<doc_id>:<n>"``: ``n`` is the 1-based ordinal of a heading
  in document order, and ``0`` is the preamble before the first heading.
  Headings inside fenced code blocks do not count. The block a ``chunk_id``
  names runs from its heading to the next heading of the same or a shallower
  level.
* The section a query hits is chosen lexically over the body, among the
  blocks a ``chunk_id`` can name (the preamble and every heading's block). A
  block scores over its first :data:`READ_WINDOW_CHARS` characters, the part a
  reader gets: each query term with a positive idf it contains adds
  ``idf * (1 + ln(count))``. The highest score wins; a tie goes to the block
  whose window is longer (more context for the same hits), then to the
  earlier one.

A leaf module: it imports nothing from the package.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from typing import NamedTuple

#: Hex digits of ``sha1(file_path)`` kept as a ``doc_id``.
DOC_ID_HEX = 10

#: Characters of a block the selector scores: the ``max_chars`` of the sections
#: reading protocol (``scripts/measure_tokens.py --flow sections``), which the
#: default read of ``get_document`` covers.
READ_WINDOW_CHARS = 2000

#: Ellipsis that marks a snippet cut inside the text.
ELLIPSIS = "…"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_CHUNK_ID_RE = re.compile(rf"^([0-9a-f]{{{DOC_ID_HEX}}}):(0|[1-9][0-9]*)$")


class Heading(NamedTuple):
    """One Markdown heading outside fenced code."""

    ordinal: int
    level: int
    text: str
    line: int


class Selection(NamedTuple):
    """The section a query hits: its ordinal, its heading text and its block."""

    ordinal: int
    section: str | None
    block: str


def doc_id(file_path: str) -> str:
    """The ``doc_id`` of a vault-relative path."""
    # The digest is an identifier, not a security control: ``doc_id`` is part
    # of the tool contract (2.0), so changing the algorithm changes every id.
    # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
    return hashlib.sha1(file_path.encode("utf-8")).hexdigest()[:DOC_ID_HEX]


def chunk_id(doc: str, ordinal: int) -> str:
    """The ``chunk_id`` of heading ``ordinal`` (``0`` = preamble) of document ``doc``."""
    return f"{doc}:{ordinal}"


def parse_chunk_id(value: str) -> tuple[str, int] | None:
    """``(doc_id, ordinal)`` of a well-formed ``chunk_id``, else ``None``."""
    match = _CHUNK_ID_RE.match(value.strip())
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def headings(body: str) -> list[Heading]:
    """Every heading of ``body`` in document order, skipping fenced code blocks."""
    found: list[Heading] = []
    fence: str | None = None
    for index, line in enumerate(body.splitlines()):
        marker = _FENCE_RE.match(line)
        if marker is not None:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        match = _HEADING_RE.match(line)
        if match is not None:
            found.append(Heading(len(found) + 1, len(match.group(1)), match.group(2).strip(), index))
    return found


def _block_end(heads: Sequence[Heading], position: int, line_count: int) -> int:
    """Line index where the block of ``heads[position]`` ends."""
    level = heads[position].level
    for later in heads[position + 1 :]:
        if later.level <= level:
            return later.line
    return line_count


def block(body: str, ordinal: int, heads: Sequence[Heading] | None = None) -> str:
    """The text a ``chunk_id`` names; ``ordinal`` must be within range."""
    lines = body.splitlines()
    heads = headings(body) if heads is None else heads
    if ordinal == 0:
        end = heads[0].line if heads else len(lines)
        return "\n".join(lines[:end]).strip()
    position = ordinal - 1
    start = heads[position].line
    return "\n".join(lines[start : _block_end(heads, position, len(lines))]).strip()


def outline(body: str, doc: str, *, limit: int) -> tuple[list[dict[str, object]], bool]:
    """The first ``limit`` headings as ``{chunk_id, section, level, chars}``, and whether more exist."""
    heads = headings(body)
    rows: list[dict[str, object]] = [
        {
            "chunk_id": chunk_id(doc, head.ordinal),
            "section": head.text,
            "level": head.level,
            "chars": len(block(body, head.ordinal, heads)),
        }
        for head in heads[:limit]
    ]
    return rows, len(heads) > limit


def query_terms(query: str) -> list[str]:
    """The query's terms as the keyword index splits them, first occurrence kept."""
    return list(dict.fromkeys(query.lower().split()))


def window_score(text: str, idf: Mapping[str, float], *, window: int = READ_WINDOW_CHARS) -> float:
    """How hard the query terms in ``idf`` hit the first ``window`` characters of ``text``.

    Each term with a positive idf the window contains adds ``idf * (1 + ln(count))``.
    """
    seen = text[:window].lower()
    return sum(weight * (1 + math.log(seen.count(term))) for term, weight in idf.items() if weight > 0 and term in seen)


def select_section(body: str, idf: Mapping[str, float], *, window: int = READ_WINDOW_CHARS) -> Selection | None:
    """The block of ``body`` whose first ``window`` characters the query terms in ``idf`` hit hardest.

    ``None`` when no block contains a term with a positive idf in its window.
    """
    heads = headings(body)
    best: tuple[tuple[float, int, int], Selection] | None = None
    for ordinal in range(len(heads) + 1):
        text = block(body, ordinal, heads)
        if not text:
            continue
        score = window_score(text, idf, window=window)
        if score <= 0:
            continue
        key = (score, min(len(text), window), -ordinal)
        if best is None or key > best[0]:
            section = heads[ordinal - 1].text if ordinal else None
            best = (key, Selection(ordinal, section, text))
    return best[1] if best is not None else None


def snippet(text: str, idf: Mapping[str, float], width: int) -> str | None:
    """At most ``width`` characters of ``text`` centred on its highest-idf query term.

    Whitespace is collapsed to single spaces; a cut is marked with
    :data:`ELLIPSIS`, counted inside ``width``. ``None`` when ``text`` holds no
    query term with a positive idf.
    """
    flat = " ".join(text.split())
    lowered = flat.lower()
    present = [(weight, term) for term, weight in idf.items() if weight > 0 and term in lowered]
    if not present or width <= 0:
        return None
    if len(flat) <= width:
        return flat
    if width < 3:
        return flat[:width]
    term = max(present, key=lambda pair: pair[0])[1]
    inner = width - 2
    centre = lowered.find(term) + len(term) // 2
    start = max(0, min(centre - inner // 2, len(flat) - inner))
    end = start + inner
    return (ELLIPSIS if start > 0 else "") + flat[start:end] + (ELLIPSIS if end < len(flat) else "")
