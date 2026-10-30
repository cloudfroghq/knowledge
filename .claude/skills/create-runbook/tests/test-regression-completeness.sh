#!/bin/bash
set -euo pipefail

# Regression test: verify that the skill is complete and all critical files exist
# This test MUST FAIL if instantiate-runbook.sh, patterns/, lib/guards.sh, or tests/ are missing
# because their absence breaks the core functionality.

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILED=0

echo "=== REGRESSION TEST: create-runbook Completeness ==="
echo "Skill root: $SKILL_ROOT"
echo

# Check critical files and directories
echo "Checking critical files..."

CRITICAL_FILES=(
  "SKILL.md"
  "checklist.md"
  "scripts/instantiate-runbook.sh"
  "scripts/validate-runbook.sh"
  "scripts/next-runbook-id.sh"
  "scripts/lib/guards.sh"
  "patterns/auth-access.md"
  "patterns/file-save-failure.md"
  "patterns/missing-files.md"
  "patterns/other.md"
  "patterns/registration-subscription.md"
  "patterns/sync-upload.md"
  "tests/run-timed-rehearsal.sh"
  "tests/run-guard-canaries.sh"
  "tests/run-instantiate-canaries.sh"
  "tests/fixtures/exmp-1234.json"
  "tests/fixtures/README.md"
)

for file in "${CRITICAL_FILES[@]}"; do
  if [ -f "$SKILL_ROOT/$file" ]; then
    echo "  ✓ $file"
  else
    echo "  ✗ MISSING: $file"
    FAILED=1
  fi
done

echo

# Run the three core test suites
echo "Running core test suites..."

echo
echo "1. run-timed-rehearsal.sh:"
if bash "$SKILL_ROOT/tests/run-timed-rehearsal.sh"; then
  echo "  ✓ PASS"
else
  echo "  ✗ FAIL (exit $?)"
  FAILED=1
fi

echo
echo "2. run-guard-canaries.sh:"
if bash "$SKILL_ROOT/tests/run-guard-canaries.sh"; then
  echo "  ✓ PASS"
else
  echo "  ✗ FAIL (exit $?)"
  FAILED=1
fi

echo
echo "3. run-instantiate-canaries.sh:"
if bash "$SKILL_ROOT/tests/run-instantiate-canaries.sh"; then
  echo "  ✓ PASS"
else
  echo "  ✗ FAIL (exit $?)"
  FAILED=1
fi

echo
if [ $FAILED -eq 0 ]; then
  echo "=== REGRESSION TEST PASSED ==="
  exit 0
else
  echo "=== REGRESSION TEST FAILED ==="
  exit 1
fi
