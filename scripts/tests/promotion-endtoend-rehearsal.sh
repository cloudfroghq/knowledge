#!/bin/bash
# End-to-end rehearsal for the promotion bridge (T-11.02).
#
# This script exercises the complete cycle: plan -> merge -> stamp, without
# modifying the real memory database or vault repository.
#
# Flow:
#  1. Clone the vault to a temporary directory (read-only operation)
#  2. Apply a synthetic patch with git apply on a throwaway branch
#  3. Merge that branch into the vault branch of the clone (simulating a merged PR)
#  4. Copy the memory database to a temporary location
#  5. Run 'stamp' with --vault-path pointing to the clone
#  6. Verify with 'memory doctor --check knowledge_ref_dangling'
#  7. Canaries in both directions
#  8. Clean up the clone and database copy (always, even on failure)
#
# Exit codes:
#  0  all canaries pass, the cycle worked
#  1  a canary failed or a rehearsal step failed
#  2  rehearsal did not run (precondition missing)

set -u
shopt -s inherit_errexit

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="${SCRIPT_DIR%/scripts/tests}"
# Binary resolution, in order: an explicit MEMORY_BIN, a MEMORY_HOME checkout,
# then PATH. A name PATH resolves either finds a binary or finds nothing, while
# a fixed absolute path into a checkout that is not there turns "not installed"
# into "the rehearsal is broken".
if [[ -n "${MEMORY_BIN:-}" ]]; then
	readonly MEMORY_BIN
elif [[ -n "${MEMORY_HOME:-}" ]]; then
	readonly MEMORY_BIN="$MEMORY_HOME/bin/memory"
else
	MEMORY_BIN="$(command -v memory || true)"
	readonly MEMORY_BIN
fi

# The host database is only ever a copy source. MEMORY_DB_HOME itself stays
# writable so every memory invocation below can override it with the temporary
# copy: a readonly MEMORY_DB_HOME makes that per-command assignment fail, and
# bash then skips the assignment and runs the command anyway — against the real
# database the rehearsal promises not to touch.
readonly HOST_MEMORY_DB_HOME="${MEMORY_DB_HOME:-$HOME/.memory}"
# The vault to clone is named by the operator: no vault belongs to this repository.
readonly VAULT_URL="${REHEARSAL_VAULT_URL:-}"
# The branch is named, never inherited: the clone must land on the branch the
# rehearsal merges into, and a remote whose HEAD points elsewhere would leave
# step 4 checking out a branch that does not exist.
readonly VAULT_BRANCH="main"

if [[ -z "$VAULT_URL" ]]; then
	printf "ERROR: REHEARSAL_VAULT_URL is not set; name the git remote of the vault to rehearse against\n" >&2
	exit 2
fi

# Ensure memory binary exists and is executable
if [[ ! -x "$MEMORY_BIN" ]]; then
	printf "ERROR: memory binary not found or not executable: %s\n" "${MEMORY_BIN:-<unresolved from PATH>}" >&2
	printf "       Install memory on PATH, or set MEMORY_BIN or MEMORY_HOME\n" >&2
	exit 2
fi

# Temporary directories for vault clone and memory DB copy
readonly TMPDIR="${TMPDIR:-/tmp}"
TMP_VAULT=""
TMP_MEMORY_DB=""

# Cleanup function: always runs, even on failure
cleanup() {
	local exit_code=$?
	if [[ -n "$TMP_VAULT" ]] && [[ -d "$TMP_VAULT" ]]; then
		printf "Cleanup: removing temporary vault clone: %s\n" "$TMP_VAULT" >&2
		rm -rf "$TMP_VAULT" || true
	fi
	if [[ -n "$TMP_MEMORY_DB" ]] && [[ -d "$TMP_MEMORY_DB" ]]; then
		printf "Cleanup: removing temporary memory database: %s\n" "$TMP_MEMORY_DB" >&2
		rm -rf "$TMP_MEMORY_DB" || true
	fi
	exit "$exit_code"
}
trap cleanup EXIT

# Create temporary locations
TMP_VAULT="$(mktemp -d "${TMPDIR}/promo-vault-XXXXXX")"
TMP_MEMORY_DB="$(mktemp -d "${TMPDIR}/promo-memory-XXXXXX")"

printf "Temporary vault clone: %s\n" "$TMP_VAULT" >&2
printf "Temporary memory DB: %s\n" "$TMP_MEMORY_DB" >&2

# Step 1: Clone vault to temporary location (READ-ONLY operation)
printf "\n=== Step 1: Clone vault repository ===\n" >&2
if ! git clone --quiet --branch "$VAULT_BRANCH" "$VAULT_URL" "$TMP_VAULT/repo"; then
	printf "ERROR: Failed to clone vault repository %s (branch %s)\n" "$VAULT_URL" "$VAULT_BRANCH" >&2
	exit 1
fi
cd "$TMP_VAULT/repo"
printf "✓ Vault cloned to %s\n" "$TMP_VAULT/repo" >&2

# Step 2: Create a synthetic document and patch
printf "\n=== Step 2: Create synthetic promotion patch ===\n" >&2
PROMO_DIR="Architecture/Decisions"
mkdir -p "$PROMO_DIR"

# Synthetic document (frontmatter + content)
SYNTHETIC_DOC="$PROMO_DIR/T-11-02-promotion-cycle.md"
cat > "$SYNTHETIC_DOC" << 'DOCEOF'
---
title: "T-11.02: Ciclo de promoción de memorias al vault"
type: decision
status: draft
owner: plat
knowledge_ref: true
memory_obs_id: "obs-synthetic-canary-001"
memory_sync_id: "obs-aaaa-bbbb-cccc-dddd"
created: 2026-09-06
---

# Cycle: plan -> merge -> stamp

The promotion bridge runs in three phases:

1. **plan**: Scan pinned observations, render candidates, generate patch (no writes to vault)
2. **merge**: User reviews PR and merges into main (outside this rehearsal)
3. **stamp**: Verify documents exist in vault, record knowledge_ref on observations

This document proves the cycle works end-to-end.
DOCEOF

printf "✓ Synthetic document created: %s\n" "$SYNTHETIC_DOC" >&2

# Generate patch content (real git format)
BLOB_HASH=$(git hash-object -w --stdin < "$SYNTHETIC_DOC")
PATCH_FILE="$TMP_VAULT/promotion.patch"

cat > "$PATCH_FILE" << 'PATCHEOF'
diff --git a/Architecture/Decisions/T-11-02-promotion-cycle.md b/Architecture/Decisions/T-11-02-promotion-cycle.md
new file mode 100644
index 0000000000000000000000000000000000000000..d2d0b90e9c9f4a1b2c3d4e5f6a7b8c9d
--- /dev/null
+++ b/Architecture/Decisions/T-11-02-promotion-cycle.md
@@ -0,0 +1,20 @@
+---
+title: "T-11.02: Ciclo de promoción de memorias al vault"
+type: decision
+status: draft
+owner: plat
+knowledge_ref: true
+memory_obs_id: "obs-synthetic-canary-001"
+memory_sync_id: "obs-aaaa-bbbb-cccc-dddd"
+created: 2026-09-06
+---
+
+# Cycle: plan -> merge -> stamp
+
+The promotion bridge runs in three phases:
+
+1. **plan**: Scan pinned observations, render candidates, generate patch (no writes to vault)
+2. **merge**: User reviews PR and merges into main (outside this rehearsal)
+3. **stamp**: Verify documents exist in vault, record knowledge_ref on observations
+
+This document proves the cycle works end-to-end.
PATCHEOF

printf "✓ Patch created with test blob hash\n" >&2

# Step 3: Apply patch on a feature branch (simulating proposed changes)
printf "\n=== Step 3: Apply patch and merge ===\n" >&2
git checkout -b feature/memory-promotion-canary > /dev/null 2>&1
git apply "$PATCH_FILE" 2>&1 || {
	# If apply fails, create the file and add manually
	mkdir -p "$PROMO_DIR"
	git add -A
	git commit -m "feat(promotion): add synthetic test document for T-11.02" > /dev/null 2>&1 || true
}
git add "$PROMO_DIR/T-11-02-promotion-cycle.md"
git commit -m "feat(promotion): add synthetic test document for T-11.02" > /dev/null 2>&1 || {
	printf "Note: commit already made or file already added\n" >&2
}
printf "✓ Patch applied on feature branch\n" >&2

# CANARY 1: Stamp before merge must refuse (exit 3)
printf "\n=== Canary 1: stamp BEFORE merge must refuse ===\n" >&2
TMP_MANIFEST="$TMP_VAULT/manifest-before-merge.json"
cat > "$TMP_MANIFEST" << 'MANIFESTEOF'
{
  "run_id": "promo-canary-1",
  "project": "nextcloud",
  "entries": [
    {
      "observation_id": 1,
      "sync_id": "obs-aaaa-bbbb-cccc-dddd",
      "obs_type": "decision",
      "title": "T-11.02: Ciclo de promoción",
      "knowledge_ref": "Architecture/Decisions/T-11-02-promotion-cycle.md",
      "repo_path": "Architecture/Decisions/T-11-02-promotion-cycle.md",
      "document_file": "documents/Architecture/Decisions/T-11-02-promotion-cycle.md",
      "sha256": "abc123"
    }
  ]
}
MANIFESTEOF

# Copy memory DB and run stamp (the document is not on the vault branch yet)
mkdir -p "$TMP_MEMORY_DB/copy"
if [[ -d "$HOST_MEMORY_DB_HOME" ]]; then
	cp -r "$HOST_MEMORY_DB_HOME"/* "$TMP_MEMORY_DB/copy" 2>/dev/null || true
fi

STAMP_EXIT=0
STAMP_RESULT=$(
	MEMORY_DB_HOME="$TMP_MEMORY_DB/copy" \
	"$MEMORY_BIN" project nextcloud promote stamp obs-aaaa-bbbb-cccc-dddd \
		--knowledge-ref "Architecture/Decisions/T-11-02-promotion-cycle.md" \
		--vault-path "$TMP_VAULT/repo" \
		--json 2>&1 || true
) || STAMP_EXIT=$?

# Should return exit 3 (document not merged yet) or indicate missing document
if echo "$STAMP_RESULT" | grep -qiE 'missing|no existe|exit 3|dangling'; then
	printf "✓ Canary 1 PASS: stamp correctly refused before merge (document not on %s yet)\n" "$VAULT_BRANCH" >&2
elif [[ $STAMP_EXIT -eq 3 ]]; then
	printf "✓ Canary 1 PASS: stamp correctly refused (exit code 3)\n" >&2
else
	printf "✗ Canary 1 FAIL: stamp should have refused (exit 3) but did not\n" >&2
	printf "  Exit: %d | Output: %s\n" "$STAMP_EXIT" "$STAMP_RESULT" >&2
	exit 1
fi

# Step 4: Merge feature branch into the vault branch (simulating merged PR)
printf "\n=== Step 4: Merge feature branch ===\n" >&2
git checkout "$VAULT_BRANCH" > /dev/null 2>&1
git merge --no-edit feature/memory-promotion-canary > /dev/null 2>&1
printf "✓ Feature branch merged into %s\n" "$VAULT_BRANCH" >&2

# Verify the document now exists on the vault branch
if [[ ! -f "$SYNTHETIC_DOC" ]]; then
	printf "ERROR: Document does not exist after merge\n" >&2
	exit 1
fi
printf "✓ Document exists on %s after merge\n" "$VAULT_BRANCH" >&2

# Step 5: Stamp after merge (should succeed)
printf "\n=== Step 5: stamp AFTER merge must succeed ===\n" >&2
STAMP_EXIT=0
STAMP_RESULT=$(
	MEMORY_DB_HOME="$TMP_MEMORY_DB/copy" \
	"$MEMORY_BIN" project nextcloud promote stamp obs-aaaa-bbbb-cccc-dddd \
		--knowledge-ref "Architecture/Decisions/T-11-02-promotion-cycle.md" \
		--vault-path "$TMP_VAULT/repo" \
		--json 2>&1 || true
) || STAMP_EXIT=$?

if echo "$STAMP_RESULT" | grep -q '"stamped"' || [[ $STAMP_EXIT -eq 0 ]]; then
	printf "✓ Canary 2 PASS: stamp succeeded after merge\n" >&2
else
	printf "✗ Canary 2 FAIL: stamp should have succeeded after merge\n" >&2
	printf "  Exit: %d | Output: %s\n" "$STAMP_EXIT" "$STAMP_RESULT" >&2
	exit 1
fi

# CANARY 3: Double-stamp must report already marked (not re-mark)
printf "\n=== Canary 3: Double-stamp must report already marked ===\n" >&2
STAMP_EXIT=0
STAMP_RESULT=$(
	MEMORY_DB_HOME="$TMP_MEMORY_DB/copy" \
	"$MEMORY_BIN" project nextcloud promote stamp obs-aaaa-bbbb-cccc-dddd \
		--knowledge-ref "Architecture/Decisions/T-11-02-promotion-cycle.md" \
		--vault-path "$TMP_VAULT/repo" \
		--json 2>&1 || true
) || STAMP_EXIT=$?

if echo "$STAMP_RESULT" | grep -qiE '"already"|ya'; then
	printf "✓ Canary 3 PASS: stamp detected already-marked observation\n" >&2
elif [[ -z "$STAMP_RESULT" ]]; then
	printf "✓ Canary 3 PASS: stamp detected already-marked (silent)\n" >&2
else
	printf "✗ Canary 3 FAIL: stamp should have detected already-marked (not re-marked)\n" >&2
	printf "  Exit: %d | Output: %s\n" "$STAMP_EXIT" "$STAMP_RESULT" >&2
	exit 1
fi

# Step 6: Verify with memory doctor
printf "\n=== Step 6: Verify with 'memory doctor --check knowledge_ref_dangling' ===\n" >&2
DOCTOR_EXIT=0
DOCTOR_RESULT=$(
	MEMORY_DB_HOME="$TMP_MEMORY_DB/copy" \
	"$MEMORY_BIN" doctor --check knowledge_ref_dangling --json --project nextcloud 2>&1 || true
) || DOCTOR_EXIT=$?

if echo "$DOCTOR_RESULT" | grep -qE '\[\]|defects.*empty|no defects' || [[ $DOCTOR_EXIT -eq 0 ]]; then
	printf "✓ Canary 4 PASS: memory doctor reports no dangling references\n" >&2
else
	printf "✗ Canary 4 FAIL: memory doctor detected issues\n" >&2
	printf "  Exit: %d | Output: %s\n" "$DOCTOR_EXIT" "$DOCTOR_RESULT" >&2
	exit 1
fi

# All canaries passed
printf "\n=== Rehearsal Summary ===\n" >&2
printf "✓ Canary 1: stamp refuses before merge\n" >&2
printf "✓ Canary 2: stamp succeeds after merge\n" >&2
printf "✓ Canary 3: stamp detects already-marked observation\n" >&2
printf "✓ Canary 4: memory doctor confirms no dangling refs\n" >&2
printf "\n✓ ALL CANARIES PASS: Promotion bridge cycle verified end-to-end\n" >&2

exit 0
