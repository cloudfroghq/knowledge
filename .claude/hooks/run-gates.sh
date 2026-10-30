#!/usr/bin/env bash
# .claude/hooks/run-gates.sh — PreToolUse for `git push` and `bkt pr create`.
#
# Ring 0 (ADR-006): fast local feedback, not authoritative. A failure here can
# be bypassed by pushing outside Claude Code; the merge is actually protected
# by Bitbucket Pipelines (ring 1). A tool that is not installed is reported as
# a WARNING and skipped -- it never turns a green run red by itself.
#
# Usage: run-gates.sh
#   Scopes every check to the commits this push adds (see scan_range). Set
#   RUN_GATES_BASE_REF to one or more space-separated refs when the branch has
#   no tracking branch yet and the generic anchors do not resolve; set
#   RUN_GATES_SKIP=1 to bypass deliberately -- that choice is printed.
#
# No `set -u`: bash 3.2 (the default /bin/bash on macOS) treats an empty
# declared array as unbound on expansion, which would abort the moment no
# relevant file changed. Every scalar below carries a `:-default`.
set -eo pipefail

# Drain (and ignore) the PreToolUse JSON payload so the caller never blocks on
# a full pipe; this hook inspects the repo, not the tool_input.
cat >/dev/null 2>&1 || true

if [ "${RUN_GATES_SKIP:-}" = "1" ]; then
  echo "run-gates: skipped on purpose (RUN_GATES_SKIP=1)"
  exit 0
fi

FAILURES=""
WARNINGS=""

echo "== ring 0: run-gates =="

# Anchors to fall back on when the branch has no tracking branch yet, most
# specific first. Nothing here names the branch of a particular product:
# refs/remotes/origin/HEAD is the remote's own answer to "what is my default
# branch", so a repository that renames its trunk, or one served from a fork,
# keeps working with no edit to this hook. The literal names come last, only
# as the answer for a checkout whose symref was never written (git clone
# --single-branch leaves none).
#
# RUN_GATES_BASE_REF overrides the whole list and accepts several refs
# separated by spaces: a series that forks off a long-lived integration branch
# needs that branch named, and naming it is a per-checkout decision, not
# something to hard-code here.
base_candidates() {
  if [ -n "${RUN_GATES_BASE_REF:-}" ]; then
    # Word splitting is the point: the variable holds a candidate list.
    # shellcheck disable=SC2086
    printf '%s\n' ${RUN_GATES_BASE_REF}
    return 0
  fi
  git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true
  printf '%s\n' 'origin/main' 'origin/master'
}

# The commit range this push actually adds, as `<anchor>..HEAD`.
#
# Anchoring on the trunk is wrong on its own: a branch that shares no ancestor
# with it -- an imported history, a series grafted from another repository --
# resolves to the entire branch, hundreds of commits of other people's work
# rescanned on every push, over this hook's own ceiling and permanently red on
# preexisting findings.
#
# The tracking branch is the honest anchor: it is exactly what the remote
# already has, so the range is exactly what the push adds. A branch that has
# never been pushed has no tracking branch yet; there the anchor is the merge
# base with the nearest candidate, nearest because a candidate unreachable
# from HEAD would drag a whole foreign history back in.
scan_range() {
  local upstream candidate base count best best_count

  if upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)" && [ -n "$upstream" ]; then
    printf '%s..HEAD' "$upstream"
    return 0
  fi

  best=""
  best_count=""
  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    git rev-parse --verify --quiet "$candidate" >/dev/null 2>&1 || continue
    base="$(git merge-base "$candidate" HEAD 2>/dev/null)" || continue
    [ -n "$base" ] || continue
    count="$(git rev-list --count "$base..HEAD" 2>/dev/null)" || continue
    if [ -z "$best_count" ] || [ "$count" -lt "$best_count" ]; then
      best="$base"
      best_count="$count"
    fi
  done < <(base_candidates)

  [ -n "$best" ] || return 1
  printf '%s..HEAD' "$best"
}

SCAN_RANGE="$(scan_range || true)"
if [ -z "$SCAN_RANGE" ]; then
  WARNINGS="${WARNINGS}no tracking branch and no reachable anchor; falling back to HEAD~1 (set RUN_GATES_BASE_REF)
"
  SCAN_RANGE="HEAD~1..HEAD"
fi
BASE_REF="${SCAN_RANGE%..HEAD}"
echo "range: ${SCAN_RANGE}"

run_step() {
  # run_step <label> <command...>
  label="$1"; shift
  echo "-- ${label} --"
  tool="$1"
  if ! command -v "$tool" >/dev/null 2>&1; then
    WARNINGS="${WARNINGS}${tool} not found locally; ${label} skipped
"
    echo "   (${tool} not installed, skipped)"
    return 0
  fi
  if ! "$@"; then
    FAILURES="${FAILURES}${label}
"
  fi
}

# Secrets first, and over the whole range rather than the changed Python
# files: a credential reaches a repository through a compose file, a fixture
# or a note far more often than through source.
echo "-- secrets (gitleaks) --"
if command -v gitleaks >/dev/null 2>&1; then
  if gitleaks detect --source . --log-opts="$SCAN_RANGE" --redact --exit-code 1 --no-banner; then
    echo "   no secret in ${SCAN_RANGE}"
  else
    FAILURES="${FAILURES}secrets (gitleaks) in ${SCAN_RANGE}
"
  fi
else
  WARNINGS="${WARNINGS}gitleaks not found locally; secret scan skipped (brew install gitleaks)
"
  echo "   (gitleaks not installed, skipped)"
fi

CHANGED="$(git diff --name-only --diff-filter=ACMR "$BASE_REF"...HEAD 2>/dev/null | rg '\.py$' || true)"

if [ -z "$CHANGED" ]; then
  echo "no changed source files in ${SCAN_RANGE}; skipping the Python gates"
else
  printf 'changed files:\n%s\n\n' "$CHANGED"
  run_step "lint (ruff)" uv run ruff check
  run_step "tests (pytest)" uv run pytest -q
fi

echo
echo "== summary =="
if [ -n "$WARNINGS" ]; then
  printf 'WARNINGS:\n%s' "$WARNINGS"
fi
if [ -n "$FAILURES" ]; then
  printf 'FAILED:\n%s' "$FAILURES"
  echo "ring 0 refuses the push; fix the above or set RUN_GATES_SKIP=1 to bypass deliberately"
  exit 2
fi
echo "ring 0 green"
exit 0
