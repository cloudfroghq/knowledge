#!/usr/bin/env bash
# run-timed-rehearsal.sh — end-to-end timed rehearsal of create-runbook, without
# ever touching the real vault, Jira, or Bitbucket.
#
# Flow exercised (all real code, no step is only printed):
#   1. next-runbook-id.sh against the REAL vault, read-only  -> RB-NNN
#   2. instantiate-runbook.sh, fed by a synthetic ticket fixture
#      (tests/fixtures/exmp-1234.json -- see its README.md for provenance and
#      the sanitization rule), writing the draft to a throwaway staging dir
#   3. guard_dest_outside_vault asserted BEFORE any write, against the staging
#      dir -- this is not decorative: see run-rehearsal-canaries.sh for the
#      branch where it is forced to fail
#   4. an EPHEMERAL vault built by copying the real vault (never the other
#      direction) into a throwaway temp dir
#   5. the draft placed at its would-be destination inside the ephemeral vault
#   6. validate-runbook.sh run with MEMORY_VAULT_ROOT pointed at the ephemeral
#      copy -- this is what avoids a false "id already used" from check 3: the
#      draft's own id is legitimately absent from the real vault, but running
#      the validator directly against the real vault as a read-only reference
#      would still make check 3 index it correctly; the ephemeral copy exists
#      instead so step 4 can also drop the draft file INTO the copy at its
#      final path and exercise check 3's filename/heading alignment the same
#      way a human would see it post-merge, with zero risk of a stray write
#      landing in the real tree if a future edit to this script gets that
#      wrong
#   7. wall-clock elapsed time measured and asserted under the 15-minute budget
#   8. the exact `bkt pr create` command printed -- NEVER executed
#
# What stays OUT of scope, on purpose (see the task's own "como_simular"):
#   - `bkt pr create` is never run: no branch is pushed, no PR is opened.
#   - The draft is never copied into the REAL vault.
#   - `status: verified` is never set: the draft is born `draft` and needs a
#     human owner to confirm the diagnosis and set `last_verified` themselves.
#   - Jira is only ever READ (the fixture was recorded ahead of time precisely
#     so this script does not need to read Jira live -- see fixtures/README.md
#     for why a frozen, sanitized fixture was chosen over a live read here).
#
# Exit 0 only if every stage above succeeded AND the 15-minute budget held.
# Bash-3.2-safe (also runs under bash 5): no arrays, no ${var,,}.
set -u

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SELF_DIR/.." && pwd)"
SCRIPTS_DIR="$SKILL_DIR/scripts"
FIXTURE="${1:-$SELF_DIR/fixtures/exmp-1234.json}"
BUDGET_SECONDS=900 # 15 minutes, per the task's acceptance criterion

# shellcheck source=../scripts/lib/guards.sh
source "$SCRIPTS_DIR/lib/guards.sh"

# Left for the caller to override; otherwise passed through empty on purpose.
# validate-runbook.sh (invoked below, inside scripts/) does its own mmdc/
# puppeteer-config auto-detection when it receives an empty value -- that
# logic stays inside scripts/, the one place this skill's deliverable is
# allowed to know about machine-local tooling paths outside the package.
MMDC="${MMDC:-}"
PUPPETEER_CFG="${PUPPETEER_CFG:-}"

STAGING=""
EPHEMERAL_PARENT=""
cleanup() {
  [[ -n "$STAGING" ]] && rm -rf "$STAGING"
  [[ -n "$EPHEMERAL_PARENT" ]] && rm -rf "$EPHEMERAL_PARENT"
}
trap cleanup EXIT

STEP_N=0
step() {
  STEP_N=$((STEP_N + 1))
  printf '\n[step %d] %s\n' "$STEP_N" "$1"
}
die() {
  printf 'run-timed-rehearsal.sh: FAIL -- %s\n' "$1" >&2
  exit 1
}

if [[ ! -f "$FIXTURE" ]]; then
  die "fixture not found: $FIXTURE"
fi

START_EPOCH="$(date +%s)"
printf 'run-timed-rehearsal.sh: start %s (budget: %ss / 15 min)\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$BUDGET_SECONDS"
printf 'fixture: %s (ticket %s)\n' "$FIXTURE" "$(jq -r .key "$FIXTURE")"

# --------------------------------------------------------------------------- #
# 1. Reserve the id against the REAL vault, read-only.
# --------------------------------------------------------------------------- #
step "next-runbook-id.sh against the real vault (read-only)"
NEXT_ID="$(bash "$SCRIPTS_DIR/next-runbook-id.sh")"; rc=$?
[[ $rc -eq 0 && -n "$NEXT_ID" ]] || die "next-runbook-id.sh exited $rc"
printf 'reserved id: %s\n' "$NEXT_ID"

# --------------------------------------------------------------------------- #
# 2. Staging dir OUTSIDE the vault, guarded explicitly before any write.
# --------------------------------------------------------------------------- #
step "create staging dir and assert it is outside the real vault"
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/create-runbook-rehearsal-staging.XXXXXX")"
# Canonicalize immediately: macOS's own $TMPDIR ends in a trailing slash, so a
# plain "$TMPDIR/name.XXXXXX" mktemp path carries a literal double slash. That
# is harmless to the filesystem (the OS collapses it) but NOT to a later exact
# string comparison of paths built two different ways from the same
# directory -- reproduced by hand: it made validate-runbook.sh's own check 3
# fail to recognize the draft as itself and report a false duplicate.
STAGING="$(cd "$STAGING" && pwd -P)"
guard_dest_outside_vault "$STAGING" "$REAL_VAULT_ROOT_DEFAULT" || die "staging dir guard tripped (see message above) -- this must never happen with a mktemp -d path"
printf 'staging dir confirmed outside the real vault: %s\n' "$STAGING"

# --------------------------------------------------------------------------- #
# 3. Instantiate the draft from the fixture.
# --------------------------------------------------------------------------- #
step "instantiate-runbook.sh from the fixture"
PATTERN="$(jq -r .pattern "$FIXTURE")"
TICKET_KEY="$(jq -r .key "$FIXTURE")"
TICKET_COUNTRY="$(jq -r .country_enum "$FIXTURE")"
TITLE="Password rejected right after reset ($TICKET_COUNTRY)"
DRAFT_PATH="$(bash "$SCRIPTS_DIR/instantiate-runbook.sh" \
  --id "$NEXT_ID" --pattern "$PATTERN" --out-dir "$STAGING" \
  --title "$TITLE" --jira-refs "$TICKET_KEY" --fixture "$FIXTURE")"
rc=$?
[[ $rc -eq 0 && -f "$DRAFT_PATH" ]] || die "instantiate-runbook.sh exited $rc"
printf 'draft written: %s\n' "$DRAFT_PATH"

CATEGORY="$(jq -r .category "$FIXTURE")"
CATEGORY_FOLDER="$(printf '%s' "$CATEGORY" | cut -c1 | tr '[:lower:]' '[:upper:]')$(printf '%s' "$CATEGORY" | cut -c2-)"

# --------------------------------------------------------------------------- #
# 4. Ephemeral vault: copy of the real vault, never the other direction.
# --------------------------------------------------------------------------- #
step "build an ephemeral vault by copying the real vault (read-only source)"
[[ -d "$REAL_VAULT_ROOT_DEFAULT" ]] || die "real vault not found at $REAL_VAULT_ROOT_DEFAULT"
EPHEMERAL_PARENT="$(mktemp -d "${TMPDIR:-/tmp}/create-runbook-rehearsal-vault.XXXXXX")"
EPHEMERAL_PARENT="$(cd "$EPHEMERAL_PARENT" && pwd -P)" # see the staging-dir comment above: strips $TMPDIR's trailing slash
EPHEMERAL_VAULT="$EPHEMERAL_PARENT/cloudfrog"
# EPHEMERAL_VAULT must NOT exist yet: `cp -R src dst` copies src's CONTENTS into
# dst only when dst already exists as a directory; when dst does not exist, dst
# becomes the copy itself. mktemp -d above created the PARENT, not this path.
cp -R "$REAL_VAULT_ROOT_DEFAULT" "$EPHEMERAL_VAULT" || die "copying the real vault into the ephemeral one failed"
guard_dest_outside_vault "$EPHEMERAL_VAULT" "$REAL_VAULT_ROOT_DEFAULT" || die "ephemeral vault resolved inside the real one -- aborting before validate-runbook.sh touches it"
printf 'ephemeral vault: %s (copied from %s)\n' "$EPHEMERAL_VAULT" "$REAL_VAULT_ROOT_DEFAULT"

mkdir -p "$EPHEMERAL_VAULT/Runbooks/$CATEGORY_FOLDER"
DEST_IN_EPHEMERAL="$EPHEMERAL_VAULT/Runbooks/$CATEGORY_FOLDER/$(basename "$DRAFT_PATH")"
cp "$DRAFT_PATH" "$DEST_IN_EPHEMERAL"
printf 'draft placed at its would-be destination inside the EPHEMERAL vault: %s\n' "$DEST_IN_EPHEMERAL"

# --------------------------------------------------------------------------- #
# 5. Validate against the ephemeral vault -- this is what avoids the false
#    "id already used" that validating straight against the real vault would
#    not actually trigger (the id is genuinely free there) but that placing
#    the SAME id in the real vault as a side effect of testing would.
# --------------------------------------------------------------------------- #
step "validate-runbook.sh against the ephemeral vault"
VALIDATE_OUT="$(MEMORY_VAULT_ROOT="$EPHEMERAL_VAULT" MMDC="$MMDC" PUPPETEER_CFG="$PUPPETEER_CFG" \
  bash "$SCRIPTS_DIR/validate-runbook.sh" "$DEST_IN_EPHEMERAL" 2>&1)"
rc=$?
printf '%s\n' "$VALIDATE_OUT"
[[ $rc -eq 0 ]] || die "validate-runbook.sh exited $rc (see output above)"
printf 'validate-runbook.sh: exit 0\n'

# --------------------------------------------------------------------------- #
# 6. Timing assertion.
# --------------------------------------------------------------------------- #
step "assert the 15-minute budget"
END_EPOCH="$(date +%s)"
ELAPSED=$((END_EPOCH - START_EPOCH))
guard_under_budget "$ELAPSED" "$BUDGET_SECONDS" || die "rehearsal took ${ELAPSED}s, budget is ${BUDGET_SECONDS}s"
printf 'elapsed: %ss (budget %ss) -- within the 15-minute target\n' "$ELAPSED" "$BUDGET_SECONDS"

# --------------------------------------------------------------------------- #
# 7. Print, never run, the PR that would follow (see open-pr's own contract:
#    destination branch, title from the first conventional commit, description
#    from the repo's PR template). This is the boundary named in "como_simular":
#    everything after this line is a human decision (bkt pr create, the merge,
#    aplicar-migracion.sh --apply, git push) -- this script performs none of it.
# --------------------------------------------------------------------------- #
step "print (never run) the PR that would follow"
SLUG="$(printf '%s' "$NEXT_ID" | tr '[:upper:]' '[:lower:]')-$(printf '%s' "$TITLE" | tr '[:upper:] ' '[:lower:]-' | rg -o -- '[a-z0-9-]+' | tr -d '\n' | cut -c1-40)"
BRANCH="docs/$SLUG"
PR_TITLE="docs(runbooks): add $NEXT_ID $(printf '%s' "$TITLE" | tr '[:upper:]' '[:lower:]')"
cat <<EOF

=== NOT EXECUTED -- printed only, per the task's scope boundary ===
git -C <clone of the vault repository> checkout -b $BRANCH   # NOT this rehearsal's ephemeral copy
git -C <clone of the vault repository> add "Runbooks/$CATEGORY_FOLDER/$(basename "$DRAFT_PATH")"
git commit -m "$PR_TITLE"
git push -u origin $BRANCH
bkt pr create --target master --title "$PR_TITLE" --description "\$(cat /tmp/pr-body-$NEXT_ID.md)"
=== end of what would run; nothing above this line was executed ===

EOF

printf 'run-timed-rehearsal.sh: PASS -- %ss elapsed, exit 0\n' "$ELAPSED"
exit 0
