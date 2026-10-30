"""Embedding providers behind :class:`~knowledge.ports.EmbeddingProvider`.

Three providers, chosen per vault by its embedding settings:

* ``openai`` (:class:`OpenAIEmbeddings`) calls the OpenAI embeddings API. The
  ``openai`` package is imported on the first call, never at import time, and
  every text is cut to ``HARD_TRUNCATE_CHARS`` inside this adapter: the cut is
  a limit of that API, not of the index, so no other provider sees it.
* ``none`` (:class:`NullEmbeddings`) carries no meaning. Every text embeds to
  the same one-dimensional vector so the store still accepts documents, and
  ``semantic`` is false so callers skip the dense path and rank by keywords.
* ``bag-of-words`` (:class:`BagOfWordsEmbeddings`) is a deterministic,
  offline provider: a normalised term-count vector over a fixed vocabulary.
  The test suite and the golden master embed with it, so its arithmetic is
  part of the recorded contract and must not change.

:class:`CachedQueryEmbedder` wraps a provider for search queries: it
normalises case and whitespace and keeps a bounded cache per instance.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any

from knowledge import errors
from knowledge.ports import EmbeddingProvider

#: The OpenAI embeddings endpoint accepts at most 8,192 tokens per input; at a
#: worst case of about two characters per token this keeps every input under it.
HARD_TRUNCATE_CHARS = 7_500

#: Terms the bag-of-words provider counts. Drawn from the fixture vault so
#: hybrid and semantic ranking keep real signal in the suite.
VOCABULARY: tuple[str, ...] = (
    "telvia", "comet", "argentina", "mexico", "otp", "login", "session",
    "outage", "middleware", "runbook", "lookup", "service", "dashboard",
    "portal", "architecture", "token", "gateway",
)


class OpenAIEmbeddings:
    """The OpenAI embeddings API, with one lazily built client per instance."""

    name = "openai"
    semantic = True
    #: Longest input sent as is; the indexer's chunk plan never produces a
    #: longer chunk, and ``embed`` truncates anything longer as a last defence.
    max_input_chars = HARD_TRUNCATE_CHARS

    def __init__(self, *, api_key: str, model: str, client: Any | None = None) -> None:
        self.model = model
        self._api_key = api_key
        self._client = client

    def __repr__(self) -> str:
        return f"OpenAIEmbeddings(model={self.model!r})"

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self._api_key)
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        safe_texts = [t[:HARD_TRUNCATE_CHARS] if len(t) > HARD_TRUNCATE_CHARS else t for t in texts]
        response = self._get_client().embeddings.create(input=safe_texts, model=self.model)
        return [list(item.embedding) for item in response.data]


class NullEmbeddings:
    """No embeddings: a constant vector, flagged as carrying no meaning."""

    name = "none"
    model = "none"
    semantic = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] for _ in texts]


class BagOfWordsEmbeddings:
    """A normalised term-count vector over ``vocabulary``.

    A text with no vocabulary hit embeds to the first basis vector instead of
    the zero vector, so cosine distance stays defined for every chunk.
    """

    name = "bag-of-words"
    model = "bag-of-words"
    semantic = True

    def __init__(self, vocabulary: tuple[str, ...] = VOCABULARY) -> None:
        self._vocabulary = vocabulary

    @property
    def vocabulary(self) -> tuple[str, ...]:
        return self._vocabulary

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            low = text.lower()
            vec = [float(low.count(word)) for word in self._vocabulary]
            norm = sum(v * v for v in vec) ** 0.5
            if norm == 0:
                vec = [1.0] + [0.0] * (len(self._vocabulary) - 1)
                norm = 1.0
            out.append([v / norm for v in vec])
        return out


class CachedQueryEmbedder:
    """Embeds search queries, cached by case- and whitespace-normalised text.

    Repeated queries within a process (including the seed-then-hop double
    lookup in ``search_deep``) hit the cache instead of the provider.
    """

    def __init__(self, provider: EmbeddingProvider, *, maxsize: int = 512) -> None:
        self.provider = provider

        def _embed_normalized(normalized: str) -> tuple[float, ...]:
            return tuple(provider.embed([normalized])[0])

        self._cached: Callable[[str], tuple[float, ...]] = lru_cache(maxsize=maxsize)(_embed_normalized)

    def embed(self, text: str) -> list[float]:
        normalized = " ".join(text.lower().split())
        return list(self._cached(normalized))


PROVIDERS: frozenset[str] = frozenset({"openai", "none", "bag-of-words"})


def build_embedder(provider: str, *, model: str = "", api_key: str = "") -> EmbeddingProvider:
    """The provider named ``provider``, or a coded error for an unknown name."""
    name = provider.strip().lower()
    if name == "openai":
        return OpenAIEmbeddings(api_key=api_key, model=model)
    if name == "none":
        return NullEmbeddings()
    if name == "bag-of-words":
        return BagOfWordsEmbeddings()
    raise errors.KnowledgeConfigError(
        errors.E_EMBEDDINGS_PROVIDER_UNKNOWN,
        f"KNOWLEDGE_EMBEDDINGS_PROVIDER must be one of {', '.join(sorted(PROVIDERS))}",
    )
