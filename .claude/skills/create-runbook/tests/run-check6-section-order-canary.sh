#!/usr/bin/env bash
# run-check6-section-order-canary.sh — canary for validate-runbook.sh check 6
# (section ordering). Generates a draft runbook, creates two mutants with
# different section reorderings, and verifies:
#   - original draft: sections present and in correct order (PASS check 6)
#   - mutant 1: sections reordered (Symptoms <-> Root Cause) (FAIL check 6)
#   - mutant 2: sections reordered (Evidence <-> Communication) (FAIL check 6)
#
# No mutation touches the original vault or any committed runbooks.

set -u

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SELF_DIR/.." && pwd)"
SCRIPTS_DIR="$SKILL_DIR/scripts"
INSTANTIATE="$SCRIPTS_DIR/instantiate-runbook.sh"
VALIDATE="$SCRIPTS_DIR/validate-runbook.sh"
# shellcheck source=../scripts/lib/guards.sh
source "$SCRIPTS_DIR/lib/guards.sh"

MMDC="${MMDC:-}"
PUPPETEER_CFG="${PUPPETEER_CFG:-}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS_N=0
FAIL_N=0
ok()  { PASS_N=$((PASS_N + 1)); printf '[ok]   %s\n' "$1"; }
bad() {
  FAIL_N=$((FAIL_N + 1))
  printf '[FAIL] %s\n' "$1"
  local line
  while IFS= read -r line; do printf '       > %s\n' "$line"; done <<< "$2"
}

# Generate a fresh draft
DRAFT_DIR="$TMP/drafts"
mkdir -p "$DRAFT_DIR"

bash "$INSTANTIATE" --id RB-600 --pattern "registration-subscription" --out-dir "$DRAFT_DIR" > /dev/null 2>&1
rc=$?
if [[ $rc -ne 0 ]]; then
  bad "check6 draft generation" "instantiate-runbook.sh failed with exit code $rc"
  printf '\nPASS: %d  FAIL: %d\n' "$PASS_N" "$FAIL_N"
  exit "$FAIL_N"
fi

# Find the generated file (it will have the title in the name)
ORIGINAL_FILE="$(find "$DRAFT_DIR" -name "RB-600*.md" | head -1)"

if [[ -z "$ORIGINAL_FILE" ]]; then
  bad "check6 draft generation" "Could not find generated RB-600*.md file"
  printf '\nPASS: %d  FAIL: %d\n' "$PASS_N" "$FAIL_N"
  exit "$FAIL_N"
fi

# Helper: reorder two sections in a file
# Arguments: source_file, output_file, section1_name, section2_name
reorder_sections() {
  local src="$1" out="$2" s1="$3" s2="$4"
  
  python3 << PYTHON
import re

with open("$src", "r") as f:
    lines = f.readlines()

# Find indices of both sections
s1_idx = None
s2_idx = None
for i, line in enumerate(lines):
    if line.strip() == "## $s1":
        s1_idx = i
    elif line.strip() == "## $s2":
        s2_idx = i

if s1_idx is None or s2_idx is None:
    print(f"Could not find sections $s1 or $s2", file=__import__('sys').stderr)
    exit(1)

# Find where each section ends (next ## or EOF)
def find_section_end(lines, start_idx):
    for i in range(start_idx + 1, len(lines)):
        if lines[i].startswith("##") and not lines[i].startswith("###"):
            return i
    return len(lines)

s1_end = find_section_end(lines, s1_idx)
s2_end = find_section_end(lines, s2_idx)

# Ensure we swap in the right order
if s1_idx > s2_idx:
    s1_idx, s2_idx = s2_idx, s1_idx
    s1_end, s2_end = s2_end, s1_end
    s1, s2 = s2, s1

# Extract sections
s1_content = lines[s1_idx:s1_end]
s2_content = lines[s2_idx:s2_end]

# Rebuild: swap the two sections
new_lines = lines[:s1_idx] + s2_content + lines[s1_end:s2_idx] + s1_content + lines[s2_end:]

with open("$out", "w") as f:
    f.writelines(new_lines)
PYTHON
}

# Test 1: Original draft should PASS check 6
printf '\n=== Test 1: Original draft (sections in order) ===\n'
MMDC="$MMDC" PUPPETEER_CFG="$PUPPETEER_CFG" bash "$VALIDATE" "$ORIGINAL_FILE" > "$TMP/validate1.txt" 2>&1
if grep -q "\\[PASS\\].*6" "$TMP/validate1.txt"; then
  ok "check6 original: sections present and in order"
else
  DETAIL="$(grep 'FAIL.*6' "$TMP/validate1.txt" || echo 'No FAIL found')"
  bad "check6 original should PASS" "$DETAIL"
fi

# Test 2: Swap Symptoms <-> Root Cause (should FAIL check 6)
printf '\n=== Test 2: Reordered (Symptoms <-> Root Cause) ===\n'
MUTANT1="$DRAFT_DIR/RB-600 Reordered Test 1.md"
reorder_sections "$ORIGINAL_FILE" "$MUTANT1" "Symptoms" "Root Cause"

MMDC="$MMDC" PUPPETEER_CFG="$PUPPETEER_CFG" bash "$VALIDATE" "$MUTANT1" > "$TMP/validate2.txt" 2>&1
if grep -q "\\[FAIL\\].*6.*orden incorrecto" "$TMP/validate2.txt"; then
  ok "check6 reordered (swap 1): correctly detects incorrect order"
else
  DETAIL="$(grep 'FAIL.*6' "$TMP/validate2.txt" || tail -3 "$TMP/validate2.txt")"
  bad "check6 reordered (swap 1) should FAIL on order" "$DETAIL"
fi

# Test 3: Swap Evidence <-> Communication (should FAIL check 6)
printf '\n=== Test 3: Reordered (Evidence <-> Communication) ===\n'
MUTANT2="$DRAFT_DIR/RB-600 Reordered Test 2.md"
reorder_sections "$ORIGINAL_FILE" "$MUTANT2" "Evidence" "Communication"

MMDC="$MMDC" PUPPETEER_CFG="$PUPPETEER_CFG" bash "$VALIDATE" "$MUTANT2" > "$TMP/validate3.txt" 2>&1
if grep -q "\\[FAIL\\].*6.*orden incorrecto" "$TMP/validate3.txt"; then
  ok "check6 reordered (swap 2): correctly detects incorrect order"
else
  DETAIL="$(grep 'FAIL.*6' "$TMP/validate3.txt" || tail -3 "$TMP/validate3.txt")"
  bad "check6 reordered (swap 2) should FAIL on order" "$DETAIL"
fi

# Summary
printf '\n=== Summary ===\n'
printf 'PASS: %d  FAIL: %d\n' "$PASS_N" "$FAIL_N"
exit "$FAIL_N"
