#!/usr/bin/env bash
# validate-type-tags-canaries.sh — canary battery for scripts/lib/validate-type-tags.py
#
# A gate is worth nothing until it has been seen failing, and worth little until
# it has been seen failing FOR THE RIGHT REASON. Every canary asserts an exact
# exit code, never "non-zero", and the ones that matter also assert a marker
# naming the document that fired — because this gate has four distinct outcomes
# and two of them are easy to confuse.
#
# What the gate MUST reject (exit 1): a `type/X` tag on a document that is not an
# X, where the `type` field establishes X as a class. The runbook template tagged
# `type/runbook` is the seed case: it is served as a runbook to anyone searching
# by tag, so an incident responder's first hit is a form with `RB-XXX` in it.
#
# What the gate MUST NOT reject: `type/overview` and `type/detail` used as the
# role axis inside a service tree. Dozens of curated documents use them
# deliberately, and a gate that demanded the two axes agree would attack the
# convention instead of the defect. That canary is the reason this gate has a
# narrow rule rather than "field and tag must match".
#
# What the gate must call "I could not look" (exit 2) rather than clean: a vault
# that is not there, a directory with no frontmatter in it, and a document whose
# frontmatter does not parse. What it must call undecided (exit 3) rather than
# clean: a tag reaching for a word the `type` field does not establish as a class.
#
# Every fixture is synthetic and built at run time under a temporary directory.
# The one canary that needs the real vault only READS it: reverting a fix to see
# the gate fire is done on a copy, never in place. There is no reflog for a
# working tree. The real vault lives in its own repository: CANARY_VAULT names a
# clone of it.
#
# Written for the bash 3.2 that macOS ships:
#   CANARY_VAULT=~/Projects/Vaults/<slug> /bin/bash scripts/tests/validate-type-tags-canaries.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
GATE="$ROOT/scripts/lib/validate-type-tags.py"
REAL_VAULT="${CANARY_VAULT:-}"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"

[ -f "$GATE" ]       || { printf 'error: gate not found: %s\n' "$GATE" >&2; exit 2; }
[ -n "$REAL_VAULT" ] || { printf 'error: set CANARY_VAULT to a clone of the vault repository\n' >&2; exit 2; }
[ -d "$REAL_VAULT" ] || { printf 'error: vault not found: %s\n' "$REAL_VAULT" >&2; exit 2; }
[ -n "$PY" ]         || { printf 'error: no python3 interpreter found\n' >&2; exit 2; }
"$PY" -c 'import yaml' 2> /dev/null || { printf 'error: PyYAML missing in %s\n' "$PY" >&2; exit 2; }

PASS_N=0
FAIL_N=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/type-tags-canaries.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

pass() { PASS_N=$((PASS_N + 1)); printf '[PASS] %s\n' "$1"; }
fail() { FAIL_N=$((FAIL_N + 1)); printf '[FAIL] %s\n' "$1"; }

# run <vault-dir>  -> sets OUT and GOT
run() {
  OUT="$("$PY" "$GATE" "$1" 2>&1)"
  GOT=$?
}

# expect_exit <label> <want-exit> <vault-dir>
expect_exit() {
  local label="$1" want="$2" dir="$3"
  run "$dir"
  if [ "$GOT" -eq "$want" ]; then
    pass "$label (exit $GOT)"
  else
    fail "$label — wanted exit $want, got $GOT"
    printf '%s\n' "$OUT" | sed 's/^/        /'
  fi
}

# expect_marker <label> <want-exit> <vault-dir> <pattern>
expect_marker() {
  local label="$1" want="$2" dir="$3" pat="$4"
  run "$dir"
  if [ "$GOT" -ne "$want" ]; then
    fail "$label — wanted exit $want, got $GOT"
    printf '%s\n' "$OUT" | sed 's/^/        /'
    return
  fi
  if printf '%s\n' "$OUT" | grep -q -- "$pat"; then
    pass "$label (exit $GOT, matched: $pat)"
  else
    fail "$label — exit $want was right but output never mentioned: $pat"
    printf '%s\n' "$OUT" | sed 's/^/        /'
  fi
}

# expect_absent <label> <vault-dir> <pattern>
# The verdict is deliberately not asserted here: this checks that a specific
# document stopped being reported, which is what "the fix holds" means. A gate
# can go on failing for other documents and this must still be provable.
expect_absent() {
  local label="$1" dir="$2" pat="$3"
  run "$dir"
  if printf '%s\n' "$OUT" | grep -q -- "$pat"; then
    fail "$label — output still reports: $pat"
    printf '%s\n' "$OUT" | grep -- "$pat" | sed 's/^/        /'
  else
    pass "$label (no longer reported: $pat)"
  fi
}

doc() { # doc <dir> <relpath> <frontmatter-body>
  mkdir -p "$(dirname "$1/$2")"
  { printf -- '---\n'; printf '%s\n' "$3"; printf -- '---\n\n# canary\n'; } > "$1/$2"
}

# --------------------------------------------------------------------------- #
# 1. Cannot look — must never be reported as clean
# --------------------------------------------------------------------------- #
expect_exit "missing vault is exit 2, not clean" 2 "$WORK/does-not-exist"

mkdir -p "$WORK/empty"
expect_exit "directory with no markdown is exit 2, not clean" 2 "$WORK/empty"

mkdir -p "$WORK/nofm"
printf '# no frontmatter here\n' > "$WORK/nofm/plain.md"
expect_exit "only documents without frontmatter is exit 2, not clean" 2 "$WORK/nofm"

mkdir -p "$WORK/broken"
doc "$WORK/broken" "ok.md" "type: runbook
tags: [type/runbook]"
doc "$WORK/broken" "second.md" "type: runbook
tags: [type/runbook]"
printf -- '---\ntype: runbook\ntags: [oops\n---\n\n# broken\n' > "$WORK/broken/bad.md"
expect_marker "unparseable frontmatter is exit 2, not clean" 2 "$WORK/broken" "UNREADABLE:"

mkdir -p "$WORK/unterminated"
doc "$WORK/unterminated" "ok.md" "type: runbook
tags: [type/runbook]"
printf -- '---\ntype: runbook\n\n# never closes\n' > "$WORK/unterminated/open.md"
expect_marker "frontmatter that never closes is exit 2" 2 "$WORK/unterminated" "never closes"

# --------------------------------------------------------------------------- #
# 2. The role axis must NOT be reported — this is what keeps the gate narrow
# --------------------------------------------------------------------------- #
mkdir -p "$WORK/roleaxis"
doc "$WORK/roleaxis" "Services/A/A.md" "type: service
tags: [type/overview, service/a]"
doc "$WORK/roleaxis" "Services/A/Endpoints.md" "type: endpoint
tags: [type/detail, service/a]"
doc "$WORK/roleaxis" "Services/B/B.md" "type: service
tags: [type/overview, service/b]"
doc "$WORK/roleaxis" "Services/B/Config.md" "type: configuration
tags: [type/detail, service/b]"
expect_exit "role axis (type/overview, type/detail) is clean, not a finding" 0 "$WORK/roleaxis"

mkdir -p "$WORK/agree"
doc "$WORK/agree" "one.md" "type: runbook
tags: [type/runbook, service/x]"
doc "$WORK/agree" "two.md" "type: runbook
tags: [type/runbook, service/y]"
doc "$WORK/agree" "three.md" "type: template
tags: [type/template]"
expect_exit "tags that agree with their type field are clean" 0 "$WORK/agree"

# --------------------------------------------------------------------------- #
# 3. The defect — an established class claimed by a tag
# --------------------------------------------------------------------------- #
mkdir -p "$WORK/collide"
doc "$WORK/collide" "rb-one.md" "type: runbook
tags: [type/runbook]"
doc "$WORK/collide" "rb-two.md" "type: runbook
tags: [type/runbook]"
doc "$WORK/collide" "rb-three.md" "type: runbook
tags: [type/runbook]"
doc "$WORK/collide" "Runbook Template.md" "type: template
tags: [type/runbook]"
expect_marker "template tagged type/runbook against 3 real runbooks is exit 1" 1 "$WORK/collide" "COLLISION: Runbook Template.md"

# Same shape, but the class is NOT established: one field use against many tag
# uses. That is a question, not a defect, and it must not be exit 0 either.
mkdir -p "$WORK/undecided"
doc "$WORK/undecided" "lone.md" "type: detail
tags: [type/detail]"
doc "$WORK/undecided" "a.md" "type: endpoint
tags: [type/detail]"
doc "$WORK/undecided" "b.md" "type: configuration
tags: [type/detail]"
doc "$WORK/undecided" "c.md" "type: architecture
tags: [type/detail]"
expect_marker "class the type field does not establish is exit 3, not clean" 3 "$WORK/undecided" "AMBIGUOUS:"

# A placeholder type ("a | b | c", as templates carry) names no class and must
# not be counted as one.
mkdir -p "$WORK/placeholder"
doc "$WORK/placeholder" "tpl.md" "type: template
service: middleware | nextcloud | lookup
tags: [type/template]"
doc "$WORK/placeholder" "real.md" "type: template
tags: [type/template]"
expect_exit "pipe-separated placeholder values establish no class" 0 "$WORK/placeholder"

# --------------------------------------------------------------------------- #
# 4. Against the real vault: the fix holds, and reverting it fires the gate
#    BY ITS CAUSE. Both run on a copy; the real vault is only ever read.
# --------------------------------------------------------------------------- #
COPY="$WORK/vault-copy"
mkdir -p "$COPY"
cp -R "$REAL_VAULT/." "$COPY/"
if [ ! -f "$COPY/Runbooks/Runbook Template.md" ]; then
  fail "vault copy is missing Runbooks/Runbook Template.md — the real-vault canaries cannot run"
else
  expect_absent "fix holds: the template is no longer served as a runbook" \
    "$COPY" "COLLISION: Runbooks/Runbook Template.md"

  # Revert the fix on the copy and require the gate to name that document.
  "$PY" - "$COPY/Runbooks/Runbook Template.md" <<'REVERT'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
raw = p.read_text(encoding="utf-8")
if "tags: [type/template," not in raw:
    sys.exit("copy does not carry the fixed tag; the revert canary would prove nothing")
p.write_text(raw.replace("tags: [type/template,", "tags: [type/runbook,", 1), encoding="utf-8")
REVERT
  if [ $? -ne 0 ]; then
    fail "could not revert the fix on the copy — the revert canary did not run"
  else
    expect_marker "reverting the template tag fires the gate by its cause" 1 "$COPY" \
      "COLLISION: Runbooks/Runbook Template.md"
  fi

  # And the analysis document: reverting its tag must name IT, not something else.
  cp -R "$REAL_VAULT/." "$COPY/" 2> /dev/null
  expect_absent "fix holds: the Jira analysis is no longer served as a runbook" \
    "$COPY" "COLLISION: Runbooks/Issue Patterns from Jira.md"
  "$PY" - "$COPY/Runbooks/Issue Patterns from Jira.md" <<'REVERT2'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
raw = p.read_text(encoding="utf-8")
if "  - type/analysis" not in raw:
    sys.exit("copy does not carry the fixed tag list; the revert canary would prove nothing")
p.write_text(raw.replace("  - type/analysis", "  - type/runbook\n  - type/analysis", 1), encoding="utf-8")
REVERT2
  if [ $? -ne 0 ]; then
    fail "could not revert the analysis tag on the copy — the revert canary did not run"
  else
    expect_marker "reverting the analysis tag fires the gate by its cause" 1 "$COPY" \
      "COLLISION: Runbooks/Issue Patterns from Jira.md"
  fi
fi

# The real vault is untouched by all of the above.
if [ -n "$(git -C "$REAL_VAULT" status --porcelain -- 'Runbooks/Runbook Template.md' | grep -v '^ M' || true)" ]; then
  fail "the canary battery left the real template in an unexpected git state"
else
  pass "the real vault was only read, never mutated by the battery"
fi

printf '\nPASS: %d  FAIL: %d\n' "$PASS_N" "$FAIL_N"
[ "$FAIL_N" -eq 0 ] || exit 1
