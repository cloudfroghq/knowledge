#!/bin/bash
set -eu

# Repository root: four levels up from .claude/skills/create-runbook/tests/.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd -P)"

cd "$REPO_ROOT"

echo "=== Regression Test: create-runbook must be versioned in git ==="
echo

# Test 1: Verify that create-runbook/SKILL.md is tracked by git
if git ls-files .claude/skills/create-runbook/SKILL.md | grep -q create-runbook; then
  echo "✓ PASS: .claude/skills/create-runbook/SKILL.md is tracked by git"
else
  echo "✗ FAIL: .claude/skills/create-runbook/SKILL.md is NOT tracked by git"
  exit 1
fi

# Test 2: Verify key scripts are tracked
for script in scripts/instantiate-runbook.sh scripts/validate-runbook.sh scripts/next-runbook-id.sh scripts/lib/guards.sh; do
  path=".claude/skills/create-runbook/$script"
  if git ls-files "$path" | grep -q "$script"; then
    echo "✓ PASS: $path is tracked"
  else
    echo "✗ FAIL: $path is NOT tracked"
    exit 1
  fi
done

# Test 3: Verify patterns are tracked
for pattern in patterns/*.md; do
  path=".claude/skills/create-runbook/$pattern"
  if git ls-files "$path" | grep -q patterns; then
    echo "✓ PASS: $path is tracked"
  else
    echo "✗ FAIL: $path is NOT tracked"
    exit 1
  fi
done

# Test 4: Verify tests are tracked
if git ls-files .claude/skills/create-runbook/tests/ | grep -q tests; then
  echo "✓ PASS: tests/ are tracked"
else
  echo "✗ FAIL: tests/ are NOT tracked"
  exit 1
fi

# Test 5: Verify CLAUDE.md is tracked
if git ls-files CLAUDE.md | grep -q CLAUDE.md; then
  echo "✓ PASS: CLAUDE.md is tracked"
else
  echo "✗ FAIL: CLAUDE.md is NOT tracked"
  exit 1
fi

echo
echo "All 5 tests PASSED. Versioning is correct."
exit 0
