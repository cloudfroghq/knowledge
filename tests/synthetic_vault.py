"""A deterministic generator of synthetic vaults, and queries with a chosen selectivity.

Shared by the performance work (``tests/test_perf_*``, ``benchmarks/``): a vault
of ``docs`` Markdown documents is written under a directory the caller owns
(a ``tmp_path``), never inside the repository and never from a real vault.
Standard library only, so a benchmark can import it against any checkout.

    manifest = generate(tmp_path / "vault", docs=200, seed=7)
    for query in queries(manifest, seed=7, n=20, selectivity=0.02):
        ...

Determinism: the same ``(docs, seed, shape)`` writes the same bytes on every
platform and Python 3.12 (``Manifest.tree_sha256`` is the proof); another seed
writes a different vault. Each document has its own ``random.Random`` seeded
from a string (SHA-512 based, stable across versions), so a document does not
depend on how long its neighbours are.

What is generated, like ``tests/fixtures/vault/`` but larger:

* frontmatter (``title``, ``type``, ``service``, ``tags``, ``status``,
  ``last_updated``; runbooks also carry ``id``), a body of ``## `` sections and
  wikilinks by name, by path, with an alias and with a heading, so the vault
  passes ``vault-lint --rules 1.3`` with no error;
* log-normal body sizes (median about 3 KB, tail to 40 KB), so about half of the
  documents have more than one chunk;
* with ``docs >= 8``: an empty body, a name with spaces and accents (NFC), a
  ``type: memory`` document under ``90 - Memory/``, a document of several
  chunks, and files inside dot-directories that the indexer must skip;
* ``Runbooks/services.json``, the services map the runbooks refer to.

Selectivity. Body words come from a Zipf vocabulary, so the commonest words are
in nearly every document and a query made of them matches most of the vault
(the roadmap measured 85 % of chunks for its first synthetic queries). The
vocabulary is built so that every word has a unique four-letter stem: a prefix
query (the server matches words of four letters or more as prefixes) matches
exactly the documents that contain that word, never an unrelated one, and the
document frequency of a word in :attr:`Manifest.doc_freq` is the exact
fraction a query on it selects. ``queries(selectivity=0.02)`` picks words whose
document frequency is near 2 %; ``selectivity=None`` draws from the Zipf
distribution like the old generator. Use :func:`measure_selectivity` to count
what a store really matches, per chunk.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

#: Structural words the generator writes besides the vocabulary: no vocabulary
#: word shares a four-letter stem with any of them.
_STRUCTURAL = (
    "service architecture flow runbook convention decision reference note pendientes memory hub "
    "diseno senales vacia notas see also details overview empty index template alias section "
    "hiddenmarker draft verified active svc"
)
_CONSONANTS = "bcdfgklmnprstvz"
_VOWELS = "aeiou"
_SUFFIX_LETTERS = "aeiolnrst"
_TOKEN = re.compile(r"[a-z]+")

#: The marker word inside files of dot-directories (which the indexer skips).
SKIPPED_MARKER = "hiddenmarker"

#: Directory of each document type; a type outside the table goes to ``Notes``.
TYPE_DIRS: Mapping[str, str] = {
    "service": "Services",
    "architecture": "Architecture",
    "flow": "Flows",
    "runbook": "Runbooks",
    "convention": "Conventions",
    "decision": "Decisions",
    "reference": "Reference",
    "hub": "Hubs",
    "note": "Notes",
    "pendientes": "Pendientes",
    "memory": "90 - Memory",
}
_TYPE_LABEL = {
    "service": "Service",
    "architecture": "Architecture",
    "flow": "Flow",
    "runbook": "Runbook",
    "convention": "Convention",
    "decision": "Decision",
    "reference": "Reference",
    "hub": "Hub",
    "note": "Note",
    "pendientes": "Pendientes",
    "memory": "Memory",
}
DEFAULT_TYPE_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("service", 0.12),
    ("architecture", 0.12),
    ("flow", 0.14),
    ("runbook", 0.12),
    ("decision", 0.12),
    ("reference", 0.16),
    ("note", 0.10),
    ("memory", 0.12),
)
SERVICES = tuple(f"svc{n}" for n in range(8))
#: Chunk size of the indexer (``chunker.ChunkPlan`` target), for the "several chunks" document.
_CHUNK_CHARS = 2500


@dataclass(frozen=True)
class Shape:
    """The statistical shape of a generated vault; the defaults reproduce the roadmap's syn500/syn5000."""

    vocab: int = 4000
    zipf_s: float = 1.07
    sections: tuple[int, int] = (3, 9)
    body_median_chars: int = 3000
    body_sigma: float = 0.9
    body_min_chars: int = 200
    body_max_chars: int = 40000
    links: tuple[int, int] = (1, 6)
    type_weights: tuple[tuple[str, float], ...] = DEFAULT_TYPE_WEIGHTS
    #: Default document frequency of the query words (``None``: Zipf-drawn, unselective).
    query_selectivity: float | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.vocab <= max_vocab():
            raise ValueError(f"vocab must be between 1 and {max_vocab()}")
        if self.sections[0] < 1 or self.sections[1] < self.sections[0]:
            raise ValueError("sections is an inclusive (min, max) with min >= 1")
        if self.links[0] < 0 or self.links[1] < self.links[0]:
            raise ValueError("links is an inclusive (min, max) with min >= 0")
        if not self.type_weights or any(weight <= 0 for _, weight in self.type_weights):
            raise ValueError("type_weights needs at least one type and positive weights")

    @classmethod
    def from_export(cls, export: Mapping[str, Any]) -> Shape:
        """A shape calibrated from a ``doctor --shape`` export (``shape_version`` 1; distributions only)."""
        if export.get("shape_version") != 1:
            raise ValueError("unsupported shape_version (expected 1)")
        defaults = cls()
        docs = max(int(export.get("docs") or 0), 1)
        changes: dict[str, Any] = {}

        body = export.get("body_chars")
        if isinstance(body, Mapping) and body.get("p50"):
            median = float(body["p50"])
            changes["body_median_chars"] = max(int(median), 1)
            p10, p90 = body.get("p10"), body.get("p90")
            if p10 and p90 and float(p90) > float(p10) > 0:
                changes["body_sigma"] = max((math.log(float(p90)) - math.log(float(p10))) / (2 * 1.2816), 0.05)
            if body.get("max"):
                changes["body_max_chars"] = max(int(body["max"]), changes["body_median_chars"])
            changes["body_min_chars"] = min(defaults.body_min_chars, changes["body_median_chars"])

        sections = export.get("sections_per_doc")
        if isinstance(sections, Mapping) and sections.get("p10") and sections.get("p90"):
            low = max(int(sections["p10"]), 1)
            changes["sections"] = (low, max(int(sections["p90"]), low))

        links = export.get("links_per_doc")
        if isinstance(links, Mapping) and links.get("p10") is not None and links.get("p90") is not None:
            low = max(int(links["p10"]), 0)
            changes["links"] = (low, max(int(links["p90"]), low))

        counts = export.get("type_counts")
        if isinstance(counts, Mapping):
            weights = tuple((str(name), float(n)) for name, n in sorted(counts.items()) if float(n) > 0)
            if weights:
                changes["type_weights"] = weights

        df = export.get("query_term_df")
        if isinstance(df, Mapping) and df.get("p50") is not None:
            value = float(df["p50"])
            changes["query_selectivity"] = min(value if value <= 1 else value / docs, 1.0)
        return replace(defaults, **changes)


def max_vocab() -> int:
    """How many distinct vocabulary words exist (one per free four-letter stem)."""
    return len(_stems())


def _stems() -> list[str]:
    forbidden = {word[:4] for word in _STRUCTURAL.split()}
    return [
        a + b + c + d
        for a in _CONSONANTS
        for b in _VOWELS
        for c in _CONSONANTS
        for d in _VOWELS
        if a + b + c + d not in forbidden
    ]


def make_vocabulary(seed: int, size: int) -> tuple[str, ...]:
    """``size`` words by rank (rank 0 is the commonest), each with a stem no other word has."""
    rng = random.Random(f"{seed}:vocab")
    stems = _stems()
    rng.shuffle(stems)
    return tuple(stem + "".join(rng.choice(_SUFFIX_LETTERS) for _ in range(rng.randint(0, 4))) for stem in stems[:size])


@dataclass(frozen=True)
class Manifest:
    """What :func:`generate` wrote."""

    root: Path
    docs: int
    seed: int
    shape: Shape
    #: Vault-relative paths of the documents the indexer reads, sorted.
    paths: tuple[str, ...]
    #: Vault-relative paths of files under dot-directories (the indexer skips them), sorted.
    skipped: tuple[str, ...]
    #: ``role -> path`` of the guaranteed special documents (empty when ``docs < 8``).
    specials: Mapping[str, str]
    #: ``type -> paths`` of the indexable documents.
    by_type: Mapping[str, tuple[str, ...]]
    #: The vocabulary by Zipf rank.
    vocabulary: tuple[str, ...]
    #: ``word -> number of documents that contain it`` (vocabulary words only).
    doc_freq: Mapping[str, int]
    #: SHA-256 over every file of the tree (sorted path, content), the byte-determinism proof.
    tree_sha256: str
    total_bytes: int
    #: Wikilinks written, as ``(source path, link text)``, for tests that count them.
    link_count: int = 0

    def fraction(self, word: str) -> float:
        """The share of documents that contain ``word``: what a query on it selects, per document."""
        return self.doc_freq.get(word, 0) / self.docs if self.docs else 0.0


def tree_sha256(root: Path) -> str:
    """SHA-256 over every file under ``root``: the sorted relative paths and their bytes."""
    digest = hashlib.sha256()
    for path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Planning: every name is known before any body is written, so links resolve.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    index: int
    type: str
    name: str
    path: str
    service: str
    role: str = ""


def _plan(docs: int, seed: int, shape: Shape) -> list[_Plan]:
    rng = random.Random(f"{seed}:plan")
    types = [name for name, _ in shape.type_weights]
    weights = [weight for _, weight in shape.type_weights]
    roles = ["empty", "accented", "memory", "multichunk"] if docs >= 8 else []
    plans: list[_Plan] = []
    runbooks = 0
    for index in range(docs):
        role = roles[index] if index < len(roles) else ""
        doc_type = rng.choices(types, weights)[0]
        if role == "empty":
            doc_type = "note"
        elif role == "accented":
            doc_type = "reference"
        elif role == "memory":
            doc_type = "memory"
        elif role == "multichunk":
            doc_type = "architecture"
        if doc_type == "runbook":
            runbooks += 1
            if runbooks > 999:  # a runbook id is RB-NNN
                doc_type = "note"
        label = _TYPE_LABEL.get(doc_type, doc_type.capitalize())
        if role == "empty":
            name = f"Nota vacia {index:05d}"
        elif role == "accented":
            name = f"Diseño de señales {index:05d}"
        else:
            name = f"{label} {index:05d}"
        name = unicodedata.normalize("NFC", name)
        directory = TYPE_DIRS.get(doc_type, "Notes")
        plans.append(_Plan(index, doc_type, name, f"{directory}/{name}.md", rng.choice(SERVICES), role))
    return plans


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _sentence(rng: random.Random, words: list[str], cum: list[float]) -> str:
    count = rng.randint(8, 16)
    return " ".join(rng.choices(words, cum_weights=cum, k=count)).capitalize() + "."


def _link(rng: random.Random, target: _Plan, headings: Mapping[str, list[str]], position: int) -> str:
    form = position % 4
    if form == 0:
        return f"[[{target.name}]]"
    if form == 1:
        return f"[[{target.path[:-3]}]]"
    if form == 2:
        return f"[[{target.name}|see also {rng.randint(1, 9)}]]"
    sections = headings.get(target.path) or []
    if sections:
        return f"[[{target.name}#{sections[rng.randrange(len(sections))]}]]"
    return f"[[{target.name}]]"


def _body_chars(rng: random.Random, shape: Shape, role: str) -> int:
    if role == "multichunk":
        return 3 * _CHUNK_CHARS + 1500
    size = int(math.exp(math.log(shape.body_median_chars) + shape.body_sigma * rng.gauss(0.0, 1.0)))
    return max(shape.body_min_chars, min(shape.body_max_chars, size))


def _headings_for(plan: _Plan, seed: int, shape: Shape, words: list[str], cum: list[float]) -> list[str]:
    rng = random.Random(f"{seed}:headings:{plan.index}")
    count = rng.randint(*shape.sections)
    if plan.role == "multichunk":
        count = max(count, 4)
    return [" ".join(rng.choices(words, cum_weights=cum, k=2)).capitalize() for _ in range(count)]


def _frontmatter(plan: _Plan, rb_id: int) -> str:
    day = date(2024, 1, 1) + timedelta(days=plan.index % 700)
    lines = ["---", f"title: {plan.name}", f"type: {plan.type}", f"service: {plan.service}"]
    if plan.type == "runbook":
        lines.append(f"id: RB-{rb_id:03d}")
        lines.append("status: verified")
    else:
        lines.append("status: active")
    lines += ["tags:", f"  - type/{plan.type}", f"  - service/{plan.service}", f"last_updated: {day.isoformat()}", "---", ""]
    return "\n".join(lines)


def generate(root: Path, *, docs: int, seed: int, shape: Shape | None = None) -> Manifest:
    """Write a vault of ``docs`` documents under ``root`` (created; it should be new and empty) and describe it."""
    if docs < 1:
        raise ValueError("docs must be at least 1")
    shape = shape or Shape()
    vocabulary = make_vocabulary(seed, shape.vocab)
    words = list(vocabulary)
    weights = [1.0 / (rank + 1) ** shape.zipf_s for rank in range(len(words))]
    cum = _cumulative(weights)

    plans = _plan(docs, seed, shape)
    headings = {plan.path: _headings_for(plan, seed, shape, words, cum) for plan in plans}
    known = set(words)
    doc_freq: dict[str, int] = {}
    link_count = 0
    rb_id = 0
    total_bytes = 0
    root.mkdir(parents=True, exist_ok=True)

    def write(rel: str, text: str) -> None:
        nonlocal total_bytes
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        target.write_bytes(data)
        total_bytes += len(data)

    for plan in plans:
        rng = random.Random(f"{seed}:doc:{plan.index}")
        if plan.type == "runbook":
            rb_id += 1
        if plan.role == "empty":
            write(plan.path, _frontmatter(plan, rb_id))
            continue
        sections = headings[plan.path]
        budget = _body_chars(rng, shape, plan.role)
        per_section = max(budget // len(sections), 60)
        link_total = rng.randint(*shape.links) if docs > 1 else 0
        link_slots = {rng.randrange(len(sections)) for _ in range(link_total)} if link_total else set()
        parts = [f"# {plan.name}", "", _sentence(rng, words, cum), ""]
        link_budget = link_total
        position = rng.randrange(4)
        for number, heading in enumerate(sections):
            parts += [f"## {heading}", ""]
            written = 0
            while written < per_section:
                text = _sentence(rng, words, cum)
                if number in link_slots and link_budget > 0 and rng.random() < 0.5:
                    other = plans[int(len(plans) * rng.random() ** 2)]
                    if other.index != plan.index:
                        text += f" See {_link(rng, other, headings, position)}."
                        position += 1
                        link_budget -= 1
                        link_count += 1
                parts.append(text)
                parts.append("")
                written += len(text) + 1
        body = "\n".join(parts)
        write(plan.path, _frontmatter(plan, rb_id) + body + "\n")
        for word in set(_TOKEN.findall(body.lower())) & known:
            doc_freq[word] = doc_freq.get(word, 0) + 1

    write(
        "Runbooks/services.json",
        '{"services": [' + ", ".join(f'{{"slug": "{slug}", "aliases": []}}' for slug in SERVICES) + "]}\n",
    )
    skipped = (
        ".obsidian/Scratch.md",
        "Architecture/.drafts/Hidden Draft.md",
    )
    for rel in skipped:
        write(rel, f"---\ntitle: Hidden\ntype: note\n---\n\n# Hidden\n\n{SKIPPED_MARKER} must never be indexed.\n")

    by_type: dict[str, list[str]] = {}
    for plan in plans:
        by_type.setdefault(plan.type, []).append(plan.path)
    return Manifest(
        root=root,
        docs=docs,
        seed=seed,
        shape=shape,
        paths=tuple(sorted(plan.path for plan in plans)),
        skipped=tuple(sorted(skipped)),
        specials={plan.role: plan.path for plan in plans if plan.role},
        by_type={name: tuple(sorted(paths)) for name, paths in sorted(by_type.items())},
        vocabulary=vocabulary,
        doc_freq=dict(sorted(doc_freq.items())),
        tree_sha256=tree_sha256(root),
        total_bytes=total_bytes,
        link_count=link_count,
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def queries(
    manifest: Manifest,
    *,
    seed: int,
    n: int,
    selectivity: float | None | str = "shape",
    terms: int = 2,
) -> list[str]:
    """``n`` deterministic queries of ``terms`` vocabulary words each.

    ``selectivity`` is the target share of documents each word is in (a query
    of ``terms`` words matches at most ``terms`` times that, as the search ORs
    its tokens): ``0.02`` picks words whose document frequency is near 2 %.
    ``None`` draws words from the Zipf distribution, the old, unselective
    behaviour (the commonest words are in almost every document). The default
    ``"shape"`` takes ``manifest.shape.query_selectivity``.
    """
    if n < 0 or terms < 1:
        raise ValueError("n must be >= 0 and terms >= 1")
    target = manifest.shape.query_selectivity if selectivity == "shape" else selectivity
    if isinstance(target, str):
        raise ValueError("selectivity is a fraction, None or 'shape'")
    rng = random.Random(f"{manifest.seed}:{seed}:queries")
    words = list(manifest.vocabulary)
    if target is None:
        weights = [1.0 / (rank + 1) ** manifest.shape.zipf_s for rank in range(len(words))]
        cum = _cumulative(weights)
        return [" ".join(_distinct(rng, words, cum, terms)) for _ in range(n)]
    if not 0 < target <= 1:
        raise ValueError("selectivity must be in (0, 1]")
    present = [word for word in words if manifest.doc_freq.get(word, 0) > 0]
    if not present:
        raise ValueError("the vault has no vocabulary word to query")
    ranked = sorted(present, key=lambda w: (abs(manifest.fraction(w) - target), w))
    band = [w for w in ranked if target * 0.7 <= manifest.fraction(w) <= target * 1.4]
    pool = band if len(band) >= terms else ranked[: max(terms * 4, 8)]
    return [" ".join(rng.sample(pool, min(terms, len(pool)))) for _ in range(n)]


def _cumulative(weights: list[float]) -> list[float]:
    cum: list[float] = []
    total = 0.0
    for weight in weights:
        total += weight
        cum.append(total)
    return cum


def _distinct(rng: random.Random, words: list[str], cum: list[float], count: int) -> list[str]:
    picked: list[str] = []
    while len(picked) < min(count, len(words)):
        word = rng.choices(words, cum_weights=cum)[0]
        if word not in picked:
            picked.append(word)
    return picked


class _Lexical(Protocol):
    def lexical(self, query: str, k: int, flt: Any = None) -> list[Any]: ...


def measure_selectivity(store: _Lexical, queries_: Iterable[str], *, documents: int) -> list[float]:
    """The share of the vault's ``documents`` each query matches, from the store's own lexical search.

    ``lexical`` returns each matching document once (its best chunk), so this is
    a per-document share, the same unit as :meth:`Manifest.fraction`. For the
    exact per-chunk row count of the FTS5 statement, read ``SqlTrace.rows_fetched``
    while running it.
    """
    return [len(store.lexical(query, documents, None)) / documents for query in queries_]
