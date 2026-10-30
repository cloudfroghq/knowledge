#!/usr/bin/env python3
"""Gate: a `type/*` tag must not claim a class the document is not.

The vault classifies documents on two independent axes and only one of them is
written down. The `type` frontmatter field says what a document *is*
(`runbook`, `service`, `template`). A `type/*` tag says, in the service trees,
what role it plays inside its service (`type/overview`, `type/detail`) — a
second axis with its own vocabulary. Both are legitimate, and a gate that
demanded they agree would attack a deliberate convention across dozens of
curated documents.

The defect is narrower: a tag `type/X` on a document whose own `type` is not X,
where `X` is *also* a value the `type` field takes elsewhere. There, asking by
tag and asking by field answer the same question differently, and readers get
whichever answer their query happened to use. A runbook template tagged
`type/runbook` is served as a runbook to anyone who searches by tag, so the
first thing an incident responder reads is `RB-XXX` and a `service` field
listing every service separated by pipes.

Three outcomes, never two:

* exit 0 — every document was read and no tag claims an established class.
* exit 1 — at least one tag claims a class established by the `type` field.
* exit 2 — the vault could not be read in full: it is missing, it holds no
  frontmatter at all, or a document's frontmatter does not parse. A report that
  does not cover the whole vault cannot certify it, and folding that into exit 0
  is the defect this gate exists to prevent.
* exit 3 — collisions exist, but only against classes the `type` field does not
  establish (a suffix used by many tags and by one or zero fields). That is a
  question about which axis owns the word, not a defect this gate can settle,
  and it is never reported as clean.

A class counts as *established* when the `type` field claims it at least twice
and more often than tags alone reach for it. One document carrying `type: detail`
does not establish `detail` as a class against twelve tags using it as a role.

Markers on stdout are stable so a canary can assert which finding fired, not
merely that something did: COLLISION, AMBIGUOUS, UNREADABLE.

Usage: validate-type-tags.py <vault-dir>
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment problem, not a vault problem
    print("UNREADABLE: PyYAML is not installed; frontmatter cannot be parsed", file=sys.stderr)
    sys.exit(2)

# Obsidian internals, plugin folders and the memory export are not
# curated documents and carry no frontmatter contract.
SKIP_PARTS = (".trash", ".obsidian", ".space", "90 - Memory")

EXIT_CLEAN, EXIT_COLLISION, EXIT_UNREADABLE, EXIT_AMBIGUOUS = 0, 1, 2, 3


def _frontmatter(raw: str) -> dict | None:
    """Parse the leading YAML block, or return None if there is not one to parse."""
    if not raw.startswith("---"):
        return None
    end = raw.find("\n---", 3)
    if end == -1:
        raise ValueError("frontmatter opens but never closes")
    parsed = yaml.safe_load(raw[3:end])
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(f"frontmatter is a {type(parsed).__name__}, not a mapping")
    return parsed


def _tags(fm: dict) -> list[str]:
    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return [t for t in tags if isinstance(t, str)]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <vault-dir>", file=sys.stderr)
        return EXIT_UNREADABLE

    vault = Path(argv[1])
    if not vault.is_dir():
        print(f"UNREADABLE: not a directory: {vault}", file=sys.stderr)
        return EXIT_UNREADABLE

    docs: list[tuple[str, str | None, list[str]]] = []
    unreadable: list[str] = []

    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault).as_posix()
        if any(part in rel for part in SKIP_PARTS):
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            unreadable.append(f"{rel}: {exc}")
            continue
        try:
            fm = _frontmatter(raw)
        except (ValueError, yaml.YAMLError) as exc:
            unreadable.append(f"{rel}: {exc}")
            continue
        if fm is None:
            continue  # genuinely no frontmatter: nothing to contradict
        tipo = fm.get("type")
        docs.append((rel, str(tipo).strip() if tipo is not None else None, _tags(fm)))

    for entry in unreadable:
        print(f"UNREADABLE: {entry}")
    if not docs:
        print(f"UNREADABLE: no document with frontmatter found under {vault}", file=sys.stderr)
        return EXIT_UNREADABLE

    # Placeholder values ("a | b | c" in a template) name no class.
    by_field = Counter(t for _, t, _ in docs if t and "|" not in t)
    by_tag_only = Counter(
        suffix
        for _, tipo, tags in docs
        for suffix in (t.split("/", 1)[1] for t in tags if t.startswith("type/"))
        if suffix != tipo
    )

    collisions: list[tuple[str, str | None, str, int]] = []
    ambiguous: list[tuple[str, str | None, str, int, int]] = []

    for rel, tipo, tags in docs:
        for tag in tags:
            if not tag.startswith("type/"):
                continue
            suffix = tag.split("/", 1)[1]
            if suffix == tipo or suffix not in by_field:
                continue  # agrees, or names the second axis
            held = by_field[suffix]
            if held >= 2 and held > by_tag_only[suffix]:
                collisions.append((rel, tipo, tag, held))
            else:
                ambiguous.append((rel, tipo, tag, held, by_tag_only[suffix]))

    print(f"read {len(docs)} documents with frontmatter under {vault}")
    print(f"type field values: {len(by_field)}   type/* suffixes reaching for another class: {len(by_tag_only)}")

    for rel, tipo, tag, held in collisions:
        print(
            f"COLLISION: {rel}\n"
            f"           declares type: {tipo} but is tagged {tag}, "
            f"and {held} documents hold type: {tag.split('/', 1)[1]} — "
            f"a search by tag serves it as one of them"
        )
    for rel, tipo, tag, held, tagged in ambiguous:
        print(
            f"AMBIGUOUS: {rel}\n"
            f"           declares type: {tipo} and is tagged {tag}; "
            f"{held} document(s) hold that type against {tagged} tag use(s) — "
            f"the field does not establish the class, so which axis owns the word is undecided"
        )

    if unreadable:
        print(f"\nUNREADABLE: {len(unreadable)} document(s) could not be parsed — this report does not cover the vault")
        return EXIT_UNREADABLE
    if collisions:
        print(f"\nFAIL: {len(collisions)} tag(s) claim an established class the document is not")
        return EXIT_COLLISION
    if ambiguous:
        print(f"\nUNDECIDED: {len(ambiguous)} tag(s) reach for a class the type field does not establish")
        return EXIT_AMBIGUOUS
    print("\nOK: every type/* tag either agrees with its type field or names the role axis")
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main(sys.argv))
