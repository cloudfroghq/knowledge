"""The pointer arithmetic behind ``doc_id``, ``chunk_id`` and the section selector.

Pure functions over a path or a Markdown body: no index, no server, no disk.
"""

from __future__ import annotations

import hashlib

import pytest

from knowledge import pointers

BODY = """Preamble line before any heading.

# Title

Intro under the title.

## Install

Run the installer.

```bash
# not a heading: inside a fenced block
echo done
```

## Rollback

### Database

Restore the snapshot of the database.

## Install

A second heading with a repeated name.
"""


def test_doc_id_is_the_sha1_prefix_of_the_path() -> None:
    path = "Runbooks/Runbook - Middleware Outage.md"

    assert pointers.doc_id(path) == hashlib.sha1(path.encode("utf-8")).hexdigest()[: pointers.DOC_ID_HEX]
    assert pointers.doc_id(path) == pointers.doc_id(path)
    assert pointers.doc_id(path) != pointers.doc_id("Services/Middleware Service.md")


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        ("d9bc623bc4:0", ("d9bc623bc4", 0)),
        ("d9bc623bc4:12", ("d9bc623bc4", 12)),
        (" d9bc623bc4:3 ", ("d9bc623bc4", 3)),
        ("d9bc623bc4", None),
        ("d9bc623bc4:-1", None),
        ("d9bc623bc4:01", None),
        ("D9BC623BC4:1", None),
        ("d9bc623b:1", None),
        ("d9bc623bc4:1:2", None),
        ("", None),
    ],
)
def test_parse_chunk_id(value: str, parsed: tuple[str, int] | None) -> None:
    assert pointers.parse_chunk_id(value) == parsed


def test_headings_skip_fenced_code_and_keep_document_order() -> None:
    heads = pointers.headings(BODY)

    assert [(h.ordinal, h.level, h.text) for h in heads] == [
        (1, 1, "Title"),
        (2, 2, "Install"),
        (3, 2, "Rollback"),
        (4, 3, "Database"),
        (5, 2, "Install"),
    ]


def test_a_tilde_fence_needs_a_tilde_close() -> None:
    body = "~~~\n# hidden\n```\n# still hidden\n~~~\n# Visible\n"

    assert [h.text for h in pointers.headings(body)] == ["Visible"]


def test_block_runs_to_the_next_heading_of_the_same_or_a_shallower_level() -> None:
    assert pointers.block(BODY, 0) == "Preamble line before any heading."
    assert pointers.block(BODY, 3) == "## Rollback\n\n### Database\n\nRestore the snapshot of the database."
    assert pointers.block(BODY, 4) == "### Database\n\nRestore the snapshot of the database."
    assert "# not a heading" in pointers.block(BODY, 2)
    assert pointers.block(BODY, 1).endswith("A second heading with a repeated name.")


def test_repeated_headings_get_distinct_chunk_ids() -> None:
    rows, more = pointers.outline(BODY, "abcdef0123", limit=30)

    install = [row for row in rows if row["section"] == "Install"]
    assert [row["chunk_id"] for row in install] == ["abcdef0123:2", "abcdef0123:5"]
    assert more is False
    assert rows[0] == {
        "chunk_id": "abcdef0123:1",
        "section": "Title",
        "level": 1,
        "chars": len(pointers.block(BODY, 1)),
    }


def test_outline_reports_truncation() -> None:
    rows, more = pointers.outline(BODY, "abcdef0123", limit=2)

    assert [row["chunk_id"] for row in rows] == ["abcdef0123:1", "abcdef0123:2"]
    assert more is True


def test_query_terms_split_like_the_keyword_index() -> None:
    assert pointers.query_terms("Rollback the  DATABASE rollback") == ["rollback", "the", "database"]


def test_a_tie_goes_to_the_block_with_the_longer_window() -> None:
    hit = pointers.select_section(BODY, {"snapshot": 1.0})

    assert hit is not None
    # "# Title", "## Rollback" and "### Database" all hold the term once, so
    # they tie; "# Title" reads the most text around it.
    assert hit.ordinal == 1
    assert hit.section == "Title"


def test_more_hits_inside_the_window_beat_a_longer_block() -> None:
    body = "# Top\n\nOne snapshot here.\n\n## Leaf\n\n" + ("snapshot restore steps " * 20) + "\n\n## Other\n\nsnapshot\n"

    hit = pointers.select_section(body, {"snapshot": 1.0, "restore": 0.5})

    assert hit is not None
    # Top holds every hit Leaf holds plus two more, inside the same window.
    assert (hit.ordinal, hit.section) == (1, "Top")


def test_only_the_window_counts() -> None:
    filler = "lorem ipsum " * 200  # 2,400 characters: past the window
    body = f"# Long\n\n{filler}\nrare term\n\n# Short\n\nrare term once\n"

    hit = pointers.select_section(body, {"rare": 2.0})

    assert hit is not None
    assert len(pointers.block(body, 1)) > pointers.READ_WINDOW_CHARS
    assert (hit.ordinal, hit.section) == (2, "Short")
    # A wider window sees the hit in the first block, which then wins the tie.
    assert pointers.select_section(body, {"rare": 2.0}, window=10_000).ordinal == 1


def test_repeated_hits_count_logarithmically() -> None:
    body = "# Many\n\n" + "common " * 50 + "\n\n# Last\n\nrare word\n"

    hit = pointers.select_section(body, {"common": 0.5, "rare": 2.0})

    assert hit is not None
    # 0.5 * (1 + ln 50) is about 2.46, more than 2.0 for one rare hit.
    assert hit.section == "Many"


def test_a_tie_on_score_and_window_goes_to_the_first_block() -> None:
    body = "# One\n\n" + "alpha " * 50 + "\n\n# Two\n\n" + "alpha " * 50 + "\n"

    hit = pointers.select_section(body, {"alpha": 1.0})

    assert hit is not None
    assert hit.ordinal == 1


def test_the_preamble_can_win_and_has_no_section() -> None:
    hit = pointers.select_section(BODY, {"preamble": 1.0})

    assert hit is not None
    assert (hit.ordinal, hit.section) == (0, None)


@pytest.mark.parametrize("idf", [{}, {"absent": 2.0}, {"snapshot": 0.0}])
def test_no_term_with_weight_selects_nothing(idf: dict[str, float]) -> None:
    assert pointers.select_section(BODY, idf) is None


def test_snippet_centres_on_the_highest_idf_term_within_width() -> None:
    text = "lead " * 40 + "rare marker here " + "tail " * 40

    cut = pointers.snippet(text, {"lead": 0.1, "rare": 3.0}, 40)

    assert cut is not None
    assert len(cut) <= 40
    assert "rare" in cut
    assert cut.startswith(pointers.ELLIPSIS) and cut.endswith(pointers.ELLIPSIS)


def test_snippet_returns_short_text_whole_with_whitespace_collapsed() -> None:
    assert pointers.snippet("Rare  word\nhere", {"rare": 1.0}, 120) == "Rare word here"


def test_snippet_without_a_term_is_none() -> None:
    assert pointers.snippet("nothing to see", {"rare": 1.0}, 120) is None
