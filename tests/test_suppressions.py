"""Every scanner suppression in the shipped code is declared here, or the suite fails.

The ring-1 SAST step prints "Findings: 0". That number is not the same claim as
"the rules found nothing to complain about": some rules still match this
repository and are silenced by ``# nosemgrep`` comments. Left undeclared, the
zero reads to the next person as "the rule stopped applying", and the cheapest
way to keep a security gate green forever is to keep adding comments.

So the suppressions are inventoried. Each entry names the file, the exact rule
it silences, why the finding is not reachable, and the test that pins the
behaviour making it unreachable. Three things fail here:

* a suppression in the code that nobody declared — the case this exists for;
* a declared suppression whose line is gone, so the inventory stops describing
  the tree it claims to describe (the contract is pinned from both sides:
  one-directional checks rot in the direction nobody watches);
* a blanket ``# nosemgrep`` with no rule id, which silences every present and
  future rule on that line, including ones nobody has read.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNED_TREES = ("src", "scripts")

# Semgrep accepts `# nosemgrep`, `#nosemgrep`, and a comma-separated rule list
# after a colon. All three forms have to be found, not just the one used here.
_SUPPRESSION = re.compile(r"#\s*nosemgrep(?::\s*(?P<rules>[^\s#]+))?")


class Declared:
    """One audited suppression."""

    def __init__(self, path: str, rule: str, why: str, pinned_by: str) -> None:
        self.path = path
        self.rule = rule
        self.why = why
        self.pinned_by = pinned_by

    @property
    def key(self) -> tuple[str, str]:
        return (self.path, self.rule)


INVENTORY: tuple[Declared, ...] = (
    Declared(
        path="src/knowledge/confluence_sync.py",
        rule="python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected",
        why=(
            "The rule fires on any urlopen() whose target is not a string literal. "
            "The Confluence base URL is configuration, so no rewrite of this call "
            "can satisfy it. The reachable risk — urlopen honouring file:// and "
            "ftp:// — is removed by the _ALLOWED_URL_SCHEMES check, which refuses "
            "every non-http(s) scheme before a Request object exists."
        ),
        pinned_by="tests/test_maintenance_tools.py::test_a_non_http_confluence_url_is_refused_before_any_request",
    ),
    Declared(
        path="scripts/promote-memories.py",
        rule="python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1",
        why=(
            "The rule fires on any hashlib.sha1() call. This one reproduces git's own "
            "blob object-id format (sha1(header + data)) so the emitted patch carries "
            "an index line `git apply` accepts. It is not a cryptographic use — the "
            "value is a content identifier, never a secret or a signature — and any "
            "other algorithm would produce an id git itself does not recognize."
        ),
        pinned_by="tests/test_promotion_bridge.py::test_patch_index_line_holds_the_real_git_blob_id",
    ),
    Declared(
        path="src/knowledge/pointers.py",
        rule="python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1",
        why=(
            "The rule fires on any hashlib.sha1() call, usedforsecurity=False included. "
            "doc_id is a stable identifier of a vault-relative path, never a secret, a "
            "signature or an integrity check, and its value is part of the tool contract "
            "(2.0): every row, chunk_id and frozen golden carries it, so another algorithm "
            "would change every id clients already hold."
        ),
        pinned_by="tests/test_pointers.py::test_doc_id_is_the_sha1_prefix_of_the_path",
    ),
    Declared(
        path="src/knowledge/judges.py",
        rule="python.lang.security.audit.httpsconnection-detected.httpsconnection-detected",
        why=(
            "The rule flags every http.client.HTTPSConnection as an audit point. The "
            "judge opens it on purpose: a direct connection to api.typesafe.ai with no "
            "proxy and no redirect, under a hard deadline. The reachable risk — a "
            "connection that skips certificate or host checks — is removed by the "
            "context from ssl.create_default_context(), which requires both."
        ),
        pinned_by="tests/test_judges.py::test_the_typesafe_connection_verifies_the_certificate_and_the_host",
    ),
)

_BY_KEY = {entry.key: entry for entry in INVENTORY}


def _found_suppressions() -> list[tuple[str, str, int]]:
    """(repo-relative path, rule id, line number) for every suppression in the shipped trees."""
    found: list[tuple[str, str, int]] = []
    for tree in SCANNED_TREES:
        root = REPO_ROOT / tree
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".sh"}:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                match = _SUPPRESSION.search(line)
                if not match:
                    continue
                rules = match.group("rules")
                if not rules:
                    found.append((rel, "", lineno))
                    continue
                for rule in rules.split(","):
                    found.append((rel, rule.strip(), lineno))
    return found


def test_no_suppression_is_undeclared() -> None:
    """A scanner comment that nobody wrote down is a silent exemption."""
    undeclared = sorted(
        f"{path}:{lineno} silences {rule or '<every rule>'}"
        for path, rule, lineno in _found_suppressions()
        if (path, rule) not in _BY_KEY
    )

    assert undeclared == [], (
        "suppression(s) missing from INVENTORY in this file; add the rule, the "
        f"reason it is unreachable, and the test that pins it: {undeclared}"
    )


def test_no_suppression_is_blanket() -> None:
    """``# nosemgrep`` with no rule id silences rules nobody has read yet."""
    blanket = sorted(
        f"{path}:{lineno}" for path, rule, lineno in _found_suppressions() if not rule
    )

    assert blanket == [], (
        f"bare `# nosemgrep` (no rule id) silences every rule on the line: {blanket}"
    )


def test_every_declared_suppression_still_exists() -> None:
    """The inventory has to describe the tree, not a tree that used to be there.

    Without this the check is one-directional: undeclared suppressions fail,
    stale entries never do, and the file slowly becomes a list of claims about
    code that changed years ago.
    """
    present = {(path, rule) for path, rule, _ in _found_suppressions()}
    stale = sorted(f"{entry.path} :: {entry.rule}" for entry in INVENTORY if entry.key not in present)

    assert stale == [], f"INVENTORY entries with no matching suppression in the tree: {stale}"


@pytest.mark.parametrize("entry", INVENTORY, ids=lambda e: e.path)
def test_every_declared_suppression_names_a_test_that_exists(entry: Declared) -> None:
    """The justification has to be backed by something that runs.

    A suppression whose reason is only prose is a promise. The named test is
    what turns "the finding is unreachable" into something the suite re-proves
    on every run.
    """
    module, _, test_name = entry.pinned_by.partition("::")
    path = REPO_ROOT / module
    assert path.is_file(), f"{entry.path}: pinned_by names a module that does not exist: {module}"
    assert test_name, f"{entry.path}: pinned_by must name a test, not just a module"
    assert f"def {test_name}(" in path.read_text(), (
        f"{entry.path}: pinned_by names a test that is not in {module}: {test_name}"
    )
    assert entry.why.strip(), f"{entry.path}: a suppression with no stated reason is not audited"
