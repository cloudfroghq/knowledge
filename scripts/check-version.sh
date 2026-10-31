#!/bin/sh
# Check that every place naming the package version agrees.
#
# Usage: scripts/check-version.sh [vX.Y.Z]
#
# Compared: `version` in pyproject.toml, `version` in
# plugin/claude-code/.claude-plugin/plugin.json, the `cloudfrog-knowledge==<version>`
# package source in plugin/claude-code/.mcp.json, `version` in npm/package.json
# (the npm wrapper runs the PyPI release of its own version) and, when uv.lock
# exists, the locked version of the project itself. The tag is compared too when it is
# given as the argument or, without an argument, when HEAD carries a `v*` tag;
# with neither, the tag check is skipped, so a release branch passes before its
# tag exists. It reads files only: whether the release is already on PyPI is not
# checked here, because publish.yml publishes it on the same tag. Read-only. Exit status: 0 when everything agrees, 1 otherwise,
# 2 on a usage error or an unreadable file.
#
# KNOWLEDGE_ROOT points the check at another checkout (the tests use it).

set -eu

root=${KNOWLEDGE_ROOT:-$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)}

if [ "$#" -gt 1 ]; then
  echo "usage: scripts/check-version.sh [vX.Y.Z]" >&2
  exit 2
fi

tag=${1:-}
if [ -z "$tag" ] && git -C "$root" rev-parse --git-dir >/dev/null 2>&1; then
  tag=$(git -C "$root" tag --points-at HEAD --list 'v*' | head -n 1)
fi

exec python3 - "$root" "$tag" <<'PY'
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

root, tag = Path(sys.argv[1]), sys.argv[2]
SEMVER = r"\d+\.\d+\.\d+"


def fail(message: str) -> None:
    print(f"check-version: {message}", file=sys.stderr)
    raise SystemExit(2)


def read(rel: str) -> str:
    path = root / rel
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        fail(f"cannot read {rel}")
    raise AssertionError


def pyproject_version() -> str:
    text = read("pyproject.toml")
    project = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    found = project and re.search(rf'^version\s*=\s*"({SEMVER})"\s*$', project.group(1), re.M)
    if not found:
        fail("pyproject.toml has no [project] version")
    return found.group(1)


def plugin_version() -> str:
    try:
        version = json.loads(read("plugin/claude-code/.claude-plugin/plugin.json"))["version"]
    except (ValueError, KeyError):
        fail("plugin.json has no version")
    return str(version)


def mcp_ref() -> str:
    found = re.findall(rf"cloudfrog-knowledge==({SEMVER})\"", read("plugin/claude-code/.mcp.json"))
    if len(found) != 1:
        fail(".mcp.json must name the package source exactly once as cloudfrog-knowledge==X.Y.Z")
    return found[0]


def npm_version() -> str:
    try:
        version = json.loads(read("npm/package.json"))["version"]
    except (ValueError, KeyError):
        fail("npm/package.json has no version")
    return str(version)


def lock_version() -> str | None:
    if not (root / "uv.lock").is_file():
        return None
    found = re.search(
        rf'^name = "cloudfrog-knowledge"\nversion = "({SEMVER})"$', read("uv.lock"), re.M
    )
    if not found:
        fail("uv.lock has no cloudfrog-knowledge package")
    return found.group(1)


seen = {
    "pyproject.toml": pyproject_version(),
    "plugin.json": plugin_version(),
    ".mcp.json": mcp_ref(),
    "npm/package.json": npm_version(),
}
locked = lock_version()
if locked is not None:
    seen["uv.lock"] = locked
if tag:
    if not re.fullmatch(rf"v{SEMVER}", tag):
        fail(f"tag {tag!r} is not vX.Y.Z")
    seen["tag"] = tag[1:]

for name, version in seen.items():
    print(f"{name}: {version}")
if len(set(seen.values())) != 1:
    print("check-version: versions disagree", file=sys.stderr)
    raise SystemExit(1)
print(f"check-version: ok ({next(iter(seen.values()))})")
PY
