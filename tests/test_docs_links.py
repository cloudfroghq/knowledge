"""Every relative link and anchor in the repository's Markdown resolves.

The documentation lives in the repository (not in a wiki), split across the
README, ``CONTRIBUTING.md`` and ``docs/``. A moved section breaks links in
other files without any test noticing, so this module reads every tracked
``*.md`` file and checks, offline and with the standard library only:

- that each relative link target exists (after URL-decoding, so ``%20`` works);
- that each ``#fragment`` on a Markdown target, or on the same file, matches a
  heading slug of that file (GitHub's rules) or an explicit ``<a id>`` /
  ``<a name>`` anchor;
- that absolute links into this repository on GitHub
  (``https://github.com/cloudfroghq/knowledge/(blob|tree)/main/<path>``, the
  repository root with a fragment, and ``raw.githubusercontent.com``) name a
  path that exists, with their fragments checked too.

Fenced code blocks, inline code and HTML comments are skipped. Fixtures and
goldens are content under test, not documentation, and are left out.
"""

from __future__ import annotations

import re
import subprocess
import unicodedata
from functools import cache
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_PREFIXES = ("tests/fixtures/", "tests/golden/")
REPO_URL = "https://github.com/cloudfroghq/knowledge"
RAW_PREFIX = "https://raw.githubusercontent.com/cloudfroghq/knowledge/main/"
# GitHub's own anchor for the README block of a repository page.
GITHUB_REPO_ANCHORS = {"readme"}

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_ANCHOR = re.compile(r"<a\s+[^>]*?\b(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)>?")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_INLINE_CODE = re.compile(r"(`+)(?:.+?)\1", re.DOTALL)
_GITHUB_PATH = re.compile(r"^https://github\.com/cloudfroghq/knowledge/(?:blob|tree)/main/([^#?]*)(?:\?[^#]*)?(?:#(.*))?$")
_GITHUB_ROOT = re.compile(r"^https://github\.com/cloudfroghq/knowledge/?(?:#(.*))?$")


def _markdown_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.md"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
        files = [f for f in out.split("\0") if f]
    except (OSError, subprocess.CalledProcessError):
        files = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.md") if ".git" not in p.parts]
    return sorted(f for f in files if not f.startswith(EXCLUDED_PREFIXES) and (ROOT / f).is_file())


def _strip_code(text: str) -> list[str]:
    """The lines of ``text`` outside fenced blocks, without comments or inline code."""
    text = _COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE.match(line)
        if fence is None and match:
            fence = match.group(1)
            lines.append("")
            continue
        if fence is not None:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not line.strip()[len(match.group(1)):].strip():
                fence = None
            lines.append("")
            continue
        lines.append(line)
    joined = _INLINE_CODE.sub(" ", "\n".join(lines))
    return joined.split("\n")


def github_slug(heading: str) -> str:
    """GitHub's anchor for a heading's text (before de-duplication)."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # links and images keep their text
    text = re.sub(r"<[^>]+>", "", text)  # inline HTML
    text = text.replace("`", "").lower()
    kept = []
    for char in text:
        category = unicodedata.category(char)
        if char in " -_" or category[0] in ("L", "N") or category == "Mn":
            kept.append(char)
    return "".join(kept).replace(" ", "-")


@cache
def anchors(rel: str) -> frozenset[str]:
    """Every fragment that resolves in ``rel``: heading slugs and explicit anchors."""
    return anchors_of((ROOT / rel).read_text(encoding="utf-8"))


def anchors_of(raw: str) -> frozenset[str]:
    """Heading slugs (de-duplicated as GitHub does) and explicit anchors of a text."""
    found: set[str] = set(_ANCHOR.findall(_COMMENT.sub("", raw)))
    seen: dict[str, int] = {}
    fence: str | None = None
    for line in raw.splitlines():
        match = _FENCE.match(line)
        if fence is None and match:
            fence = match.group(1)
            continue
        if fence is not None:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
            continue
        heading = _HEADING.match(line)
        if not heading:
            continue
        slug = github_slug(heading.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return frozenset(found)


def _targets(line: str) -> list[str]:
    """Inline link and image targets of one line, plus a reference definition."""
    targets: list[str] = []
    reference = _REFERENCE.match(line)
    if reference:
        targets.append(reference.group(1))
    i = 0
    while True:
        i = line.find("](", i)
        if i < 0:
            return targets
        j = i + 2
        while j < len(line) and line[j] == " ":
            j += 1
        if j < len(line) and line[j] == "<":
            end = line.find(">", j)
            if end > 0:
                targets.append(line[j + 1 : end])
            i = j
            continue
        depth, k = 0, j
        while k < len(line):
            char = line[k]
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            elif char.isspace():
                break
            k += 1
        if k > j:
            targets.append(line[j:k])
        i = k


def _check_fragment(target_rel: str, fragment: str) -> str | None:
    if not fragment or not target_rel.endswith(".md"):
        return None
    if unquote(fragment) in anchors(target_rel):
        return None
    return f"no heading or anchor #{fragment} in {target_rel}"


def _check_repo_path(path: str, fragment: str | None) -> str | None:
    path = unquote(path).rstrip("/")
    if path and not (ROOT / path).exists():
        return f"{path} does not exist"
    return _check_fragment(path, fragment or "")


def problem(source: str, target: str) -> str | None:
    """Why ``target``, linked from ``source``, does not resolve; ``None`` if it does."""
    if target.startswith(RAW_PREFIX):
        return _check_repo_path(target[len(RAW_PREFIX) :].split("#")[0].split("?")[0], None)
    match = _GITHUB_PATH.match(target)
    if match:
        return _check_repo_path(match.group(1), match.group(2))
    match = _GITHUB_ROOT.match(target)
    if match:
        fragment = match.group(1)
        if fragment in GITHUB_REPO_ANCHORS:
            return None
        return _check_fragment("README.md", fragment or "")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("//"):
        return None  # another site or scheme: not checked offline
    path, _, fragment = target.partition("#")
    path = path.split("?")[0]
    if not path:
        return _check_fragment(source, fragment)
    base = ROOT if path.startswith("/") else (ROOT / source).parent
    resolved = (base / unquote(path).lstrip("/")).resolve()
    if not resolved.exists():
        return f"{path} does not exist"
    try:
        rel = resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return f"{path} leaves the repository"
    return _check_fragment(rel, fragment)


def broken_links(source: str) -> list[str]:
    text = (ROOT / source).read_text(encoding="utf-8")
    out = []
    for number, line in enumerate(_strip_code(text), start=1):
        for target in _targets(line):
            reason = problem(source, target)
            if reason:
                out.append(f"{source}:{number}: ({target}) {reason}")
    return out


FILES = _markdown_files()


def test_the_checker_sees_the_documentation() -> None:
    assert {"README.md", "CONTRIBUTING.md", "docs/install.md"} <= set(FILES)
    assert not [f for f in FILES if f.startswith(EXCLUDED_PREFIXES)]


@pytest.mark.parametrize("source", FILES)
def test_every_link_resolves(source: str) -> None:
    broken = broken_links(source)
    assert not broken, "\n".join(broken)


def test_the_link_published_on_npm_reaches_a_readme_heading() -> None:
    """npm/README.md is already on npmjs.com, so its link to the README's
    ``#configuration`` can never be retargeted: the heading has to stay."""
    assert "configuration" in anchors("README.md")
    assert problem("npm/README.md", f"{REPO_URL}#configuration") is None


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Quick start (offline, about 5 minutes)", "quick-start-offline-about-5-minutes"),
        ("Tests and merge gates (ring 1)", "tests-and-merge-gates-ring-1"),
        ("Profiles and `features`", "profiles-and-features"),
        ("`sqlite-vec` and the Python fallback", "sqlite-vec-and-the-python-fallback"),
        ("`vaults.yaml`", "vaultsyaml"),
        ("weekly-stale-report.py — Stale documents report, by owner", "weekly-stale-reportpy--stale-documents-report-by-owner"),
        ("Configuración básica", "configuración-básica"),
        ("snake_case and [a link](x.md)", "snake_case-and-a-link"),
    ],
)
def test_slugs_follow_github(heading: str, slug: str) -> None:
    assert github_slug(heading) == slug


def test_duplicate_headings_get_a_suffix() -> None:
    text = '# Setup\n\n## Setup\n\n```\n# Setup\n```\n\n## Setup\n<a id="old-name"></a>\n'
    assert anchors_of(text) == {"setup", "setup-1", "setup-2", "old-name"}


def test_code_is_not_read_as_links() -> None:
    lines = _strip_code("see `[x](missing.md)`\n```md\n[y](missing.md)\n```\n<!-- [z](missing.md) -->\n[ok](README.md)\n")
    assert [t for line in lines for t in _targets(line)] == ["README.md"]
