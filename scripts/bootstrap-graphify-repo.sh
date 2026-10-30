#!/usr/bin/env bash
# bootstrap-graphify-repo.sh — onboard one repository checkout onto graphify, up to and
# including a labelled graph, without touching any shared or machine-global file.
#
# Usage:
#   bootstrap-graphify-repo.sh [--target <path>] [--force]
#
#   --target <path>  Repository root to bootstrap (default: current directory).
#   --force          Rebuild even when the graph already matches HEAD.
#   -h, --help       Print this help.
#
# Exit codes:
#   0  the repository has a graph built at its current HEAD
#   1  a step ran and its result could not be verified
#   2  bad usage, or a required dependency is missing
#
# Scope: this script writes .graphifyignore, appends graphify-out/ to .gitignore, builds
# and labels the graph. It deliberately stops there. Registering the MCP server writes to
# ~/.claude.json, which is machine-global and personal, so the exact command is PRINTED at
# the end with this repository's own graph path resolved into it, and the operator runs it.
# Copying another repository's registration instead would answer every query from the wrong
# graph with no visible error.
#
# The graphify hooks are NOT installed here either. They already live in the centralized
# context (the vault's Contexts/<slug>/.claude/settings.local.json), shared by symlink and resolving
# the repository at runtime. `graphify install`, `graphify claude install` and
# `graphify install --project` are never run: all three write .claude/settings.json and
# CLAUDE.md relative to the process working directory, which means the shared, versioned
# files of whatever tree the operator happened to be standing in.
set -euo pipefail

TARGET_DIR="$(pwd)"
FORCE=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target)
      [ "$#" -ge 2 ] || { echo "error: --target needs a path" >&2; exit 2; }
      TARGET_DIR="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h | --help)
      sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "error: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

for dep in graphify git python3 rg; do
  command -v "$dep" > /dev/null 2>&1 || {
    echo "error: '$dep' is required and is not on PATH" >&2
    exit 2
  }
done

[ -d "$TARGET_DIR" ] || { echo "error: target directory does not exist: $TARGET_DIR" >&2; exit 2; }
TARGET_DIR="$(cd "$TARGET_DIR" && pwd -P)"

git -C "$TARGET_DIR" rev-parse --git-dir > /dev/null 2>&1 || {
  echo "error: not a git checkout: $TARGET_DIR" >&2
  exit 2
}

GRAPH_JSON="$TARGET_DIR/graphify-out/graph.json"
GRAPH_REPORT="$TARGET_DIR/graphify-out/GRAPH_REPORT.md"
HEAD_COMMIT="$(git -C "$TARGET_DIR" rev-parse HEAD)"

note() { printf 'bootstrap-graphify-repo: %s\n' "$1"; }

# Prints the built_at_commit of a graph, or nothing when the file is absent or unreadable.
graph_commit() {
  [ -f "$1" ] || return 0
  python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(json.load(fh).get("built_at_commit") or "")
except Exception:
    pass
' "$1"
}

# --------------------------------------------------------------------------- #
# 1. .graphifyignore
#
# .graphifyignore MERGES with .gitignore rather than replacing it, and that merge is the
# whole reason this file is written: a rule that lives only in .gitignore never excludes a
# path Git already tracks, so committed vendor trees and build output reach the graph until
# the same rule is repeated here.
#
# Only the block between the two markers belongs to this script. Anything a human added
# outside it survives every run, because the per-repository rules that matter most (the
# generated directory one app happens to commit) can only be found by reading that
# repository.
# --------------------------------------------------------------------------- #
IGNORE_FILE="$TARGET_DIR/.graphifyignore"
BEGIN_MARKER="# >>> bootstrap-graphify-repo baseline >>>"
END_MARKER="# <<< bootstrap-graphify-repo baseline <<<"

read -r -d '' BASELINE_RULES << 'RULES' || true
# Dependencies and build output that repositories here commit into the tree.
vendor/
Vendor/
vendor-bin/
node_modules/
dist/
build/
.venv/
__pycache__/
*.pyc

# Bundler output committed next to its source: the hash in the name is the bundler's
# cache-busting fingerprint, never something anyone edits.
*.min.js
*.chunk.mjs

# Generated translation catalogues: one file per locale, emitted from the source strings.
l10n/

.git/
graphify-out/
RULES

IGNORE_VERDICT="$(
  python3 - "$IGNORE_FILE" "$BEGIN_MARKER" "$END_MARKER" "$BASELINE_RULES" << 'PY'
import sys
from pathlib import Path

path, begin, end, rules = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
block = begin + "\n" + rules.strip("\n") + "\n" + end + "\n"
existing = path.read_text(encoding="utf-8") if path.is_file() else ""

if begin in existing and end in existing:
    head, _, rest = existing.partition(begin)
    body, _, tail = rest.partition(end)
    if begin + body + end + "\n" == block:
        print("unchanged")
        sys.exit(0)
    updated = head + block + tail.lstrip("\n")
else:
    prefix = existing if (not existing or existing.endswith("\n")) else existing + "\n"
    updated = (prefix + "\n" if prefix else "") + block

path.write_text(updated, encoding="utf-8")
print("written")
PY
)"

if [ "$IGNORE_VERDICT" = "unchanged" ]; then
  note ".graphifyignore baseline already current"
else
  note ".graphifyignore baseline written"
fi

# --------------------------------------------------------------------------- #
# 2. graphify-out/ in .gitignore
# --------------------------------------------------------------------------- #
GITIGNORE_FILE="$TARGET_DIR/.gitignore"
if [ -f "$GITIGNORE_FILE" ] && rg -q '^graphify-out/?$' "$GITIGNORE_FILE"; then
  note "graphify-out/ already ignored"
else
  [ ! -s "$GITIGNORE_FILE" ] || [ "$(tail -c 1 "$GITIGNORE_FILE")" = "" ] || printf '\n' >> "$GITIGNORE_FILE"
  printf 'graphify-out/\n' >> "$GITIGNORE_FILE"
  note "graphify-out/ appended to .gitignore"
fi

# --------------------------------------------------------------------------- #
# 3 and 4. Build and label
#
# A graph whose built_at_commit already is HEAD describes this checkout, so rebuilding it
# is minutes of work for an identical artifact. --force is the way to ask for it anyway.
# --------------------------------------------------------------------------- #
EXISTING_COMMIT="$(graph_commit "$GRAPH_JSON")"

if [ "$FORCE" -eq 0 ] && [ -n "$EXISTING_COMMIT" ] && [ "$EXISTING_COMMIT" = "$HEAD_COMMIT" ]; then
  note "already up to date (built_at_commit $HEAD_COMMIT); skipping build and label"
else
  if [ "$FORCE" -eq 1 ]; then
    # `graphify update` leaves the artifacts alone when it detects no topology change, even
    # with its own --force, so a real re-extraction needs the previous run's state gone.
    rm -f "$GRAPH_JSON" "$TARGET_DIR/graphify-out/manifest.json"
  fi

  note "building the graph (this can take several minutes on a large repository)"
  (cd "$TARGET_DIR" && graphify update .)

  note "labelling communities"
  (cd "$TARGET_DIR" && graphify label .)
fi

# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
FAILED=0
for artifact in "$GRAPH_JSON" "$GRAPH_REPORT"; do
  [ -f "$artifact" ] || { echo "FAIL: graphify produced no $artifact" >&2; FAILED=1; }
done

BUILT_COMMIT="$(graph_commit "$GRAPH_JSON")"
if [ -z "$BUILT_COMMIT" ]; then
  echo "FAIL: graph.json carries no built_at_commit; staleness cannot be detected" >&2
  FAILED=1
elif [ "$BUILT_COMMIT" != "$HEAD_COMMIT" ]; then
  echo "FAIL: graph built at $BUILT_COMMIT but HEAD is $HEAD_COMMIT" >&2
  FAILED=1
fi

[ "$FAILED" -eq 0 ] || exit 1

note "graph ready at $GRAPH_JSON (commit $HEAD_COMMIT)"

# --------------------------------------------------------------------------- #
# The MCP registration command, printed and never run.
# --------------------------------------------------------------------------- #
GRAPHIFY_MCP="$(command -v graphify-mcp 2> /dev/null || true)"
[ -n "$GRAPHIFY_MCP" ] || GRAPHIFY_MCP="$HOME/.local/bin/graphify-mcp"

cat << EOF

Register the MCP server for this repository by running, from inside $TARGET_DIR:

  claude mcp add graphify -s local -- $GRAPHIFY_MCP --graph $GRAPH_JSON --transport stdio

Scope is local on purpose: the path above exists on this machine only.
EOF
