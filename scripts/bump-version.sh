#!/bin/sh
# Move the package version everywhere it is written, in one commit's worth of edits.
#
# Usage: scripts/bump-version.sh X.Y.Z [--no-lock]
#
# Writes X.Y.Z into `version` of pyproject.toml, `version` of
# plugin/claude-code/.claude-plugin/plugin.json, the `@vX.Y.Z` ref of the
# package source in plugin/claude-code/.mcp.json and `version` of
# npm/package.json, then refreshes uv.lock with
# `uv lock` (skipped with --no-lock) and runs scripts/check-version.sh. It never
# commits and never tags: the tag is cut on the merge of the release pull
# request, so the plugin can name a tag that does not exist yet.
#
# KNOWLEDGE_ROOT points the bump at another checkout (the tests use it).

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
root=${KNOWLEDGE_ROOT:-$(CDPATH='' cd -- "$here/.." && pwd)}

usage() {
  echo "usage: scripts/bump-version.sh X.Y.Z [--no-lock]" >&2
  exit 2
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
version=$1
lock=1
if [ "$#" -eq 2 ]; then
  [ "$2" = "--no-lock" ] || usage
  lock=0
fi
case $version in
  v*) echo "bump-version: pass X.Y.Z without the leading v" >&2; exit 2 ;;
esac

python3 - "$root" "$version" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

root, version = Path(sys.argv[1]), sys.argv[2]
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    print(f"bump-version: {version!r} is not X.Y.Z", file=sys.stderr)
    raise SystemExit(2)


def rewrite(rel: str, pattern: str, replacement: str, flags: int = 0) -> None:
    path = root / rel
    text = path.read_text(encoding="utf-8")
    new, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        print(f"bump-version: {rel}: nothing to replace", file=sys.stderr)
        raise SystemExit(2)
    path.write_text(new, encoding="utf-8")


# Only the [project] table's own version line: the pattern stops at the next table.
rewrite(
    "pyproject.toml",
    r'(^\[project\]\s*$(?:(?!^\[).)*?^version\s*=\s*")[^"]*(")',
    rf"\g<1>{version}\g<2>",
    re.M | re.S,
)
rewrite(
    "plugin/claude-code/.claude-plugin/plugin.json",
    r'("version"\s*:\s*")[^"]*(")',
    rf"\g<1>{version}\g<2>",
)
rewrite(
    "plugin/claude-code/.mcp.json",
    r'(knowledge\.git@v)\d+\.\d+\.\d+(")',
    rf"\g<1>{version}\g<2>",
)
# The first "version" key of the manifest is the package's own.
rewrite(
    "npm/package.json",
    r'("version"\s*:\s*")[^"]*(")',
    rf"\g<1>{version}\g<2>",
)
PY

if [ "$lock" -eq 1 ]; then
  (cd "$root" && uv lock --quiet)
fi

KNOWLEDGE_ROOT=$root "$here/check-version.sh"
