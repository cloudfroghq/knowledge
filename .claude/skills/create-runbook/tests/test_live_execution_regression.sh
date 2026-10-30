#!/bin/bash
set -eu

# Repository root: four levels up from .claude/skills/create-runbook/tests/.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)"
SKILL_ROOT="$REPO_ROOT/.claude/skills/create-runbook"

cd "$REPO_ROOT"

echo "=== Regression Test: create-runbook live execution (RB-007) ===" 
echo

# Test 1: Draft file exists in vault
# The vault lives in its own repository: MEMORY_VAULT_ROOT (or VAULT_PATH) names a clone of it.
VAULT_ROOT="${MEMORY_VAULT_ROOT:-${VAULT_PATH:-}}"
[ -n "$VAULT_ROOT" ] || { echo "✗ SKIP: set MEMORY_VAULT_ROOT to a clone of the vault repository"; exit 2; }
DRAFT_PATH="$VAULT_ROOT/Runbooks/Network/RB-007 Test: create-runbook execution in live environment - timeout on large downloads.md"
if [ -f "$DRAFT_PATH" ]; then
  echo "✓ PASS: Runbook file exists"
else
  echo "✗ FAIL: Runbook file NOT found"
  exit 1
fi

# Test 2: File contains required frontmatter
if grep -q "^id: RB-007" "$DRAFT_PATH"; then
  echo "✓ PASS: File contains id: RB-007"
else
  echo "✗ FAIL: File does NOT contain id: RB-007"
  exit 1
fi

# Test 3: File contains EXMP-1235 reference
if grep -q "EXMP-1235" "$DRAFT_PATH"; then
  echo "✓ PASS: File contains EXMP-1235 reference"
else
  echo "✗ FAIL: File does NOT contain EXMP-1235 reference"
  exit 1
fi

# Test 4: Validation script passes (exit 0)
echo && echo "Running full validation..."
DRAFT_ABS="$(pwd)/$DRAFT_PATH"
cd "$SKILL_ROOT"
if bash scripts/validate-runbook.sh "$DRAFT_ABS" > /tmp/validation_output.txt 2>&1; then
  if grep -q "Resultado: TODO PASS" /tmp/validation_output.txt; then
    echo "✓ PASS: validate-runbook.sh returns TODO PASS (exit 0)"
  else
    echo "✗ FAIL: Validation output does not contain TODO PASS"
    cat /tmp/validation_output.txt | tail -5
    exit 1
  fi
else
  echo "✗ FAIL: validate-runbook.sh exited with non-zero code"
  cat /tmp/validation_output.txt | tail -10
  exit 1
fi

# Test 5: Local commit exists
cd "$REPO_ROOT"
if git log --oneline | grep -q "RB-007"; then
  echo "✓ PASS: Local commit exists for RB-007"
else
  echo "✗ FAIL: Local commit NOT found"
  exit 1
fi

# Test 6: File is tracked by git
if git ls-files "$DRAFT_PATH" | grep -q RB-007; then
  echo "✓ PASS: File is tracked by git"
else
  echo "✗ FAIL: File is NOT tracked by git"
  exit 1
fi

echo
echo "=== All 6 tests PASSED. Live execution validated. ==="
exit 0
