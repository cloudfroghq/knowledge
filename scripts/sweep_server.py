#!/usr/bin/env python3
"""Launch ``knowledge.server`` over stdio with an experimental chunker installed.

The hyperparameter sweep (``scripts/sweep_hyperparams.py``) needs the same
server, the same tools and the same index, with one thing changed: how a
document is cut into chunks before it is embedded. This launcher replaces
``VaultIndexer._chunk`` in the server process and then starts the server, so
nothing in ``src/`` carries experiment code.

Environment:

* ``KNOWLEDGE_SWEEP_CHUNK_CHARS``: target chunk size in characters. Unset or
  ``0`` keeps the server's own chunker (``knowledge.chunker`` under the
  provider's plan): the baseline row.
* ``KNOWLEDGE_SWEEP_OVERLAP``: fraction of the target size repeated from the
  end of one chunk at the start of the next (``0.10`` = 10 %).
* ``KNOWLEDGE_SWEEP_INNER``: optional path of another launcher to run after
  the patch instead of ``knowledge.server`` (the suite passes
  ``tests/golden_stdio_server.py`` to embed with the bag-of-words double).

Both sides of the hybrid search see the patched chunks: the indexer writes
each chunk's vector and its FTS5 row from the same cut. Only the selector's idf
(``chunk_id``, ``section``, ``snippet``) reads whole bodies. ``sized_chunks`` is
re-exported from ``knowledge.chunker``, where the measured chunker lives.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from knowledge.chunker import sized_chunks  # noqa: E402 - after the src path insert

__all__ = ["install_chunker", "main", "sized_chunks"]

CHUNK_ENV = "KNOWLEDGE_SWEEP_CHUNK_CHARS"
OVERLAP_ENV = "KNOWLEDGE_SWEEP_OVERLAP"
INNER_ENV = "KNOWLEDGE_SWEEP_INNER"


def install_chunker(target: int, overlap: float) -> None:
    from knowledge.chunker import Chunk, ChunkPlan, chunks
    from knowledge.indexer import VaultIndexer

    plan = ChunkPlan(target=target, overlap=overlap)

    def _chunk(self: VaultIndexer, content: str) -> list[Chunk]:
        del self
        return chunks(content, plan)

    VaultIndexer._chunk = _chunk  # type: ignore[method-assign]


def main() -> None:
    target = int(os.environ.get(CHUNK_ENV) or 0)
    overlap = float(os.environ.get(OVERLAP_ENV) or 0)
    if target < 0 or not 0 <= overlap < 1:
        raise SystemExit(f"sweep server: invalid {CHUNK_ENV}={target} or {OVERLAP_ENV}={overlap}")
    if target:
        install_chunker(target, overlap)

    inner = os.environ.get(INNER_ENV)
    if inner:
        inner_path = Path(inner).resolve()
        sys.path.insert(0, str(inner_path.parent))
        runpy.run_path(str(inner_path), run_name="__main__")
        return

    from knowledge import server

    server.main()


if __name__ == "__main__":
    main()
