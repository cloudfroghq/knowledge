#!/usr/bin/env bash
# verify-check6-regression.sh — regression test for check 6 (section ordering)
# This test FAILS when the check 6 validation is disabled, proving that the
# check is essential and actually used by the canary suite.
#
# Usage: bash verify-check6-regression.sh
# Exit 0: check 6 is present and enforced
# Exit non-zero: check 6 is missing or disabled

set -u

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_DIR="$SKILL_DIR/scripts"
VALIDATE="$SCRIPTS_DIR/validate-runbook.sh"
CANARY="$(dirname "${BASH_SOURCE[0]}")/run-check6-section-order-canary.sh"
BACKUP="/tmp/validate-runbook.sh.regression-backup.$$"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; [[ -f "$BACKUP" ]] && cp "$BACKUP" "$VALIDATE" && rm "$BACKUP"' EXIT

# Precondition: both files exist
[[ -f "$VALIDATE" ]] || { printf 'FAIL: validate-runbook.sh not found\n' >&2; exit 1; }
[[ -x "$CANARY" ]] || { printf 'FAIL: run-check6-section-order-canary.sh not found\n' >&2; exit 1; }

# Phase 1: canary must pass with check 6 enabled
printf '[Phase 1] Running check 6 canary with check 6 enabled...\n'
if bash "$CANARY" > "$TMP/phase1.txt" 2>&1; then
  printf '[ok] Canary passes\n'
else
  printf '[FAIL] Canary should pass with check 6 enabled\n' >&2
  cat "$TMP/phase1.txt" >&2
  exit 1
fi

# Phase 2: canary must FAIL when check 6 is disabled
printf '[Phase 2] Disabling check 6 and re-running canary...\n'
cp "$VALIDATE" "$BACKUP"

# Disable check 6 by replacing the ordering assertion with 'elif false'
sd 'elif \[\[ "\$filtered" != "\$expected_subseq" \]\]; then' \
   'elif false; then # REGRESSION: check 6 disabled' "$VALIDATE" 2>/dev/null || {
  printf '[FAIL] Could not disable check 6 in validate-runbook.sh\n' >&2
  exit 1
}

if ! bash "$CANARY" > "$TMP/phase2.txt" 2>&1; then
  printf '[ok] Canary FAILS when check 6 is disabled (as expected)\n'
  printf '[PASS] Regression test passed: check 6 is necessary\n'
  exit 0
else
  printf '[FAIL] Canary should FAIL when check 6 is disabled\n' >&2
  cat "$TMP/phase2.txt" >&2
  exit 1
fi
