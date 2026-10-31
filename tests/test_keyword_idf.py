"""``KeywordIndex.idf`` equals the substring definition it has always had, to the last bit.

The reference below is a frozen copy of ``KeywordIndex.idf`` and of how
``from_bodies`` filled it at ``a89cd5e``: each document is its body (or the
empty-body chunk) lowercased, and a term's document frequency is the number of
documents that contain the lowercased term as a substring. The pointers' section
selector weighs query terms with it (``server._query_idf``), so any difference
would move a section pointer.

Terms are drawn with a seeded ``random.Random`` (no extra dependency) from the
fixture vault, a generated vault and an adversarial corpus: Unicode whitespace
that ``str.split`` honours (NBSP, em space, ``\\x1c``-``\\x1f``, ``\\x85``,
``\\u2028``), characters it does not (zero-width space), case mappings that
change length or depend on context (``İ``, ``ß``, final ``Σ``), combining
accents, punctuation and empty bodies. Whole runs, prefixes, suffixes, infixes,
upper-cased variants, terms absent from every document, terms with whitespace
and the empty term are all checked, with ``==`` on the float.
"""

from __future__ import annotations

import math
import random
import shutil
from collections.abc import Iterable
from pathlib import Path

import pytest
from synthetic_vault import generate

from knowledge.chunker import EMPTY_BODY
from knowledge.embeddings import build_embedder
from knowledge.indexer import KeywordIndex, VaultIndexer

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"
TERMS = 5000


# ---------------------------------------------------------------------------
# The frozen reference
# ---------------------------------------------------------------------------


class ReferenceIdf:
    def __init__(self, bodies: Iterable[tuple[str, str]]) -> None:
        self.docs: dict[str, str] = {}
        for file_path, body in bodies:
            self.docs[file_path] = (body if body.strip() else EMPTY_BODY).lower()
        self.doc_count = len(self.docs)

    def idf(self, term: str) -> float:
        needle = term.lower()
        df = sum(1 for d in self.docs.values() if needle in d)
        return math.log((self.doc_count + 1) / (df + 1))


# ---------------------------------------------------------------------------
# Corpora and terms
# ---------------------------------------------------------------------------

WHITESPACE = "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0        　"
NOT_WHITESPACE = "​‌‍⁠﻿"
TRICKY = "İıßẞΣσςǅǄǆŉΐ́̈ÉéÅÅﬁﬀ·-_/.,;:!?()[]{}#*`'\"«»¿¡€$%&@|\\^~0123456789"

ADVERSARIAL: list[tuple[str, str]] = [
    ("Empty.md", ""),
    ("Blank.md", " \n\t 　 "),
    ("Nbsp.md", "foo\xa0bar baz qux\x1cquux\x1d\x1e\x1fend"),
    ("Zero width.md", "zero​width joined‍word and﻿bom"),
    ("Case.md", "İstanbul STRASSE straße ΟΔΟΣ ΣΟΦΙΑ Σ ǅemal ŉ ΐ ﬁle ﬀ"),
    ("Sigma.md", "ΑΣ ΑΣΑ ΣΑ Σ. ΜΑΣ, ΟΣ!"),
    ("Accents.md", "café café naïve naïve Ångström Ångström sesión"),
    ("Punct.md", "a,b.c;d:e!f?g (paren) [brack] {brace} **bold** `code` #tag --dash a/b/c"),
    ("Lines.md", "line sep para graph next\x85line\r\nwindows\x0bvt\x0cff"),
    ("Long.md", "x" * 5000 + " " + "ab" * 1000 + " tail"),
    ("Repeat.md", "word word word WORD Word wordword words sword"),
    ("Mixed.md", "Middleware gateway OTP sesión autenticación flujo-de-login/v2 deploy:prod"),
]


def _runs(bodies: list[tuple[str, str]]) -> list[str]:
    runs: list[str] = []
    for _, body in bodies:
        runs.extend(body.split())
    return runs or ["x"]


def _terms(bodies: list[tuple[str, str]], seed: int, n: int = TERMS) -> list[str]:
    """Whole runs, slices of runs, raw slices, case variants, unknown strings, whitespace and the empty term."""
    rng = random.Random(seed)
    runs = _runs(bodies)
    texts = [body for _, body in bodies if body] or ["x"]
    alphabet = TRICKY + WHITESPACE + NOT_WHITESPACE + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out: list[str] = ["", " ", "\xa0", "​", EMPTY_BODY, EMPTY_BODY.upper(), EMPTY_BODY.strip("()")]
    while len(out) < n:
        run = rng.choice(runs)
        kind = rng.randrange(12)
        if kind == 0:
            term = run
        elif kind == 1:
            term = run[: rng.randint(1, len(run))]
        elif kind == 2:
            term = run[rng.randrange(len(run)):]
        elif kind == 3:
            i = rng.randrange(len(run))
            term = run[i : i + rng.randint(1, 6)]
        elif kind == 4:
            term = run.upper()
        elif kind == 5:
            term = run.swapcase()[: rng.randint(1, len(run))]
        elif kind == 6:  # a raw slice of a body: may span whitespace (the fallback path)
            text = rng.choice(texts)
            i = rng.randrange(len(text))
            term = text[i : i + rng.randint(1, 12)]
        elif kind == 7:
            term = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 4)))
        elif kind == 8:
            term = f"{run} {rng.choice(runs)}"
        elif kind == 9:
            term = run + rng.choice(NOT_WHITESPACE) + rng.choice(runs)
        elif kind == 10:
            term = run + "zzqq"
        else:
            term = rng.choice(TRICKY) + run[:3]
        out.append(term)
    return out


def _assert_equal(index: KeywordIndex, reference: ReferenceIdf, terms: list[str]) -> None:
    assert index.doc_count == reference.doc_count
    mismatches = [(t, index.idf(t), reference.idf(t)) for t in terms if index.idf(t) != reference.idf(t)]
    assert mismatches == []
    # Asked again (memoized or not), the answer does not change.
    assert [index.idf(t) for t in terms[:500]] == [reference.idf(t) for t in terms[:500]]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2])
def test_the_adversarial_corpus(seed: int) -> None:
    index = KeywordIndex.from_bodies(iter(ADVERSARIAL))
    _assert_equal(index, ReferenceIdf(ADVERSARIAL), _terms(ADVERSARIAL, seed))


def test_every_whitespace_character_is_handled_like_the_substring_scan() -> None:
    index = KeywordIndex.from_bodies(iter(ADVERSARIAL))
    reference = ReferenceIdf(ADVERSARIAL)
    terms = [*WHITESPACE, *NOT_WHITESPACE, *TRICKY]
    terms += [f"foo{c}bar" for c in WHITESPACE + NOT_WHITESPACE] + [f"{c}end" for c in WHITESPACE]
    _assert_equal(index, reference, terms)


def test_a_corpus_without_documents() -> None:
    index = KeywordIndex.from_bodies(iter([]))
    reference = ReferenceIdf([])
    _assert_equal(index, reference, ["", "a", "a b", "(empty)"])
    assert index.idf("anything") == math.log(1)


def _indexed(vault: Path, index_dir: Path) -> VaultIndexer:
    indexer = VaultIndexer.open(vault_path=vault, index_dir=index_dir, embedder=build_embedder("none"))
    indexer.index()
    return indexer


def test_the_fixture_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    indexer = _indexed(vault, tmp_path / "index")
    try:
        bodies = list(indexer.store.bodies())
        reference = ReferenceIdf(bodies)
        _assert_equal(indexer.keyword_index, reference, _terms(bodies, seed=3))
        _assert_equal(KeywordIndex.from_bodies(iter(bodies)), reference, _terms(bodies, seed=4, n=1000))
    finally:
        indexer.store.close()  # type: ignore[attr-defined]


def test_a_generated_vault(tmp_path: Path) -> None:
    manifest = generate(tmp_path / "vault", docs=200, seed=7)
    indexer = _indexed(manifest.root, tmp_path / "index")
    try:
        bodies = list(indexer.store.bodies())
        reference = ReferenceIdf(bodies)
        terms = _terms(bodies, seed=5) + list(manifest.vocabulary[:300]) + [w[:4] for w in manifest.vocabulary[:300]]
        _assert_equal(indexer.keyword_index, reference, terms)
    finally:
        indexer.store.close()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# How the index is built: a reload source, a repeated path, a later add
# ---------------------------------------------------------------------------


def test_with_a_reload_source_the_bodies_are_read_again_only_when_asked() -> None:
    reads: list[int] = []

    def reload() -> Iterable[tuple[str, str]]:
        reads.append(1)
        return iter(ADVERSARIAL)

    index = KeywordIndex.from_bodies(iter(ADVERSARIAL), reload=reload)
    reference = ReferenceIdf(ADVERSARIAL)
    _assert_equal(index, reference, [t for t in _terms(ADVERSARIAL, seed=6, n=1000) if t.split() == [t]])
    assert reads == [], "terms without whitespace never need the bodies"
    assert index.docs == reference.docs and reads == [1]
    _assert_equal(index, reference, _terms(ADVERSARIAL, seed=7, n=1000))
    assert reads == [1]


def test_a_repeated_path_keeps_its_last_body_as_before() -> None:
    bodies = [*ADVERSARIAL, ("Mixed.md", "only gateway now"), ("Empty.md", "no longer empty")]
    for reload in (None, lambda: iter(bodies)):
        index = KeywordIndex.from_bodies(iter(bodies), reload=reload)
        _assert_equal(index, ReferenceIdf(bodies), _terms(bodies, seed=8, n=1000))


def test_add_after_a_build_counts_the_new_document() -> None:
    index = KeywordIndex.from_bodies(iter(ADVERSARIAL), reload=lambda: iter(ADVERSARIAL))
    terms = _terms(ADVERSARIAL, seed=9, n=500)
    assert [index.idf(t) for t in terms] == [ReferenceIdf(ADVERSARIAL).idf(t) for t in terms]
    index.add("Extra.md", "Gateway STRASSE café extra")
    reference = ReferenceIdf([*ADVERSARIAL, ("Extra.md", "Gateway STRASSE café extra")])
    _assert_equal(index, reference, terms + ["gateway", "extra", "strasse"])
    empty = KeywordIndex()
    empty.add("a.md", "Alpha beta")
    _assert_equal(empty, ReferenceIdf([("a.md", "Alpha beta")]), ["alpha", "ALP", "beta gamma", "", "z"])
