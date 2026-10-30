#!/usr/bin/env bash
# run-instantiate-canaries.sh — canary battery for scripts/instantiate-runbook.sh
# and its interaction with scripts/validate-runbook.sh. Every ephemeral vault
# built here is a throwaway `cp -R` of the REAL vault (read-only source, never
# written back to) or a bare synthetic tree -- nothing here ever touches
# the vault clone it copies from.
#
# Each numbered check below has BOTH a branch that must fail and a branch that
# must pass; several are pinned against the real defect this script's sibling
# (instantiate-runbook.sh) shipped with and fixed while this rehearsal was
# built: an embedded double quote in a symptom or title broke the generated
# frontmatter's YAML outright (case 6).
#
# Bash-3.2-safe: no arrays, plain counters, no ${var,,}. Tools required: the
# same ones instantiate-runbook.sh and validate-runbook.sh need (yq, jq, sd,
# rg, fd, mmdc). Usage: bash run-instantiate-canaries.sh
set -u

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SELF_DIR/.." && pwd)"
SCRIPTS_DIR="$SKILL_DIR/scripts"
INSTANTIATE="$SCRIPTS_DIR/instantiate-runbook.sh"
VALIDATE="$SCRIPTS_DIR/validate-runbook.sh"
# shellcheck source=../scripts/lib/guards.sh
source "$SCRIPTS_DIR/lib/guards.sh"

# Left for the caller to override; otherwise passed through empty on purpose.
# validate-runbook.sh (invoked below, inside scripts/) does its own mmdc/
# puppeteer-config auto-detection when it receives an empty value -- that
# logic stays inside scripts/, the one place this skill's deliverable is
# allowed to know about machine-local tooling paths outside the package.
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

# fresh_ephemeral_vault — copies the REAL vault into a new throwaway dir under
# $TMP and prints its path. Read-only source, one-way copy, same pattern
# run-timed-rehearsal.sh uses.
fresh_ephemeral_vault() {
  local n="$1" parent vault
  parent="$(mktemp -d "$TMP/vault-$n.XXXXXX")"
  vault="$parent/cloudfrog"
  cp -R "$REAL_VAULT_ROOT_DEFAULT" "$vault"
  printf '%s\n' "$vault"
}

printf 'bash: %s\n\n' "$BASH_VERSION"

# --------------------------------------------------------------------------- #
# 1. Argument validation -- each missing/malformed input has its own exit code.
# --------------------------------------------------------------------------- #
out="$("$INSTANTIATE" --pattern auth-access --out-dir "$TMP/x1" 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && ok "missing --id exits 2" || bad "missing --id must exit 2" "got $rc: $out"

out="$("$INSTANTIATE" --id RB-1 --pattern auth-access --out-dir "$TMP/x2" 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && ok "a malformed id (RB-1, not RB-NNN) exits 2" || bad "a malformed id must exit 2" "got $rc: $out"

out="$("$INSTANTIATE" --id RB-900 --pattern not-a-real-pattern --out-dir "$TMP/x3" 2>&1)"; rc=$?
[[ $rc -eq 5 ]] && ok "an unknown pattern exits 5 (guard_pattern_known)" || bad "an unknown pattern must exit 5" "got $rc: $out"

out="$("$INSTANTIATE" --id RB-900 --pattern other --out-dir "$TMP/x4" 2>&1)"; rc=$?
[[ $rc -eq 2 ]] && ok "pattern \"other\" without --category/--service exits 2" || bad "pattern \"other\" without --category/--service must exit 2" "got $rc: $out"

out="$("$INSTANTIATE" --id RB-900 --pattern auth-access --out-dir "$REAL_VAULT_ROOT_DEFAULT/Runbooks/canary-attempt" 2>&1)"; rc=$?
[[ $rc -eq 3 ]] && ok "--out-dir under the real vault exits 3 (guard_dest_outside_vault)" || bad "--out-dir under the real vault must exit 3" "got $rc: $out"
[[ -d "$REAL_VAULT_ROOT_DEFAULT/Runbooks/canary-attempt" ]] && bad "the guard fired but the directory got created anyway" "found $REAL_VAULT_ROOT_DEFAULT/Runbooks/canary-attempt"

# The positive case every negative case above needs as a control: a well-formed
# call must actually succeed.
GOOD_DIR="$TMP/good"
mkdir -p "$GOOD_DIR"
DRAFT="$("$INSTANTIATE" --id RB-900 --pattern auth-access --out-dir "$GOOD_DIR" --title 'Canary draft' 2>/dev/null)"; rc=$?
if [[ $rc -eq 0 && -f "$DRAFT" ]]; then
  ok "a well-formed call exits 0 and writes the draft"
else
  bad "a well-formed call must exit 0 and write the draft" "got $rc, draft=$DRAFT"
fi

# --------------------------------------------------------------------------- #
# 2. Regression: an embedded double quote in symptom/title must not break the
#    generated frontmatter's YAML. This is the EXMP-1234 fixture bug, pinned
#    down with a minimal fixture so it never comes back silently.
# --------------------------------------------------------------------------- #
QUOTE_FIXTURE="$TMP/quote-fixture.json"
jq -n '{key:"EXMP-1",summary:"canary summary text",country_enum:"MX",created:"2026-01-01",issue_type:"Issue productivo",
        symptoms:["User sees: \"Incorrect username or password\" right after a reset","second symptom"]}' > "$QUOTE_FIXTURE"
QUOTE_DIR="$TMP/quote-out"
mkdir -p "$QUOTE_DIR"
QUOTE_DRAFT="$("$INSTANTIATE" --id RB-901 --pattern auth-access --out-dir "$QUOTE_DIR" --title 'Title with an embedded "quote" in it' --fixture "$QUOTE_FIXTURE" 2>/dev/null)"
if [[ -f "$QUOTE_DRAFT" ]]; then
  fm="$(frontmatter_json_of "$QUOTE_DRAFT")"
  if [[ -n "$fm" ]] && printf '%s' "$fm" | jq -e '.symptoms | length == 2' > /dev/null 2>&1; then
    ok "a symptom and title with an embedded double quote still produce valid, complete YAML"
  else
    bad "a symptom/title with an embedded double quote must still produce valid YAML" "frontmatter_json_of returned: $fm"
  fi
else
  bad "the quote-fixture instantiation must succeed" "draft not found"
fi

# --------------------------------------------------------------------------- #
# 3. validate-runbook.sh, both directions, against ephemeral (never real) vaults.
# --------------------------------------------------------------------------- #
V1="$(fresh_ephemeral_vault v1)"
mkdir -p "$V1/Runbooks/Auth"
GOOD_IN_V1="$V1/Runbooks/Auth/$(basename "$DRAFT")"
cp "$DRAFT" "$GOOD_IN_V1"
out="$(MEMORY_VAULT_ROOT="$V1" MMDC="$MMDC" PUPPETEER_CFG="$PUPPETEER_CFG" bash "$VALIDATE" "$GOOD_IN_V1" 2>&1)"; rc=$?
[[ $rc -eq 0 ]] && ok "a well-formed draft passes validate-runbook.sh (exit 0) -- the control every FAIL case below is compared against" \
  || bad "a well-formed draft must pass validate-runbook.sh" "$out"

# 3a. Missing required field -> check 1 FAIL.
V2="$(fresh_ephemeral_vault v2)"
mkdir -p "$V2/Runbooks/Auth"
MISSING_FIELD="$V2/Runbooks/Auth/$(basename "$DRAFT")"
rg -v '^owner: ' "$DRAFT" > "$MISSING_FIELD"
out="$(MEMORY_VAULT_ROOT="$V2" MMDC="$MMDC" PUPPETEER_CFG="$PUPPETEER_CFG" bash "$VALIDATE" "$MISSING_FIELD" 2>&1)"; rc=$?
if [[ $rc -eq 1 ]] && [[ "${out#*FAIL] 1 }" != "$out" ]]; then
  ok "a draft missing a required field (owner) fails check 1 (overall exit 1)"
else
  bad "a draft missing 'owner' must fail check 1" "got $rc: $out"
fi

# 3b. Id already used in the vault -> check 3 FAIL. Seed V3 with a second file
# carrying the SAME id as the good draft before validating the good draft
# against it.
V3="$(fresh_ephemeral_vault v3)"
mkdir -p "$V3/Runbooks/Auth"
SEEDED_DRAFT_ID="$(rg -o --no-filename '^id: (RB-[0-9]+)$' -r '$1' "$DRAFT" | head -1)"
cat > "$V3/Runbooks/Auth/$SEEDED_DRAFT_ID Already Used.md" <<EOF
---
type: runbook
id: $SEEDED_DRAFT_ID
title: "Already used"
service: middleware
severity: P2
category: auth
pattern: auth-access
status: draft
owner: middleware-team
automation_level: manual
last_verified: 2026-01-01
last_updated: 2026-01-01
last_occurrence: 2026-01-01
symptoms: ["a", "b"]
root_cause: "x"
resolution_time: "x"
affected_services: []
affected_endpoints: []
affected_tables: []
affected_databases: []
jira_project: EXMP
jira_issuetype: "Issue productivo"
jira_issuetype_id: "1"
jira_refs: []
country: [ALL]
partner: [none]
tools: [curl]
evidence_required: [manifest]
tags: []
---

# $SEEDED_DRAFT_ID: Already used

## Symptoms

- x

## Root Cause

x

## Affected Components

| Component | Type | Details |
|-----------|------|---------|
| x | x | x |

## Preconditions

| Requirement | How to verify | Tool |
|-------------|---------------|------|
| x | x | x |

## Solution Flow

1. x

## Validation Flow

1. x

## Rollback

| Step | Rollback command | Irreversible? |
|------|------------------|---------------|
| x | x | no |

## Evidence

| Artifact | Captures | Tool | File name |
|----------|----------|------|-----------|
| x | x | x | x |

## Communication

x

## Prevention

- x

## Related Issues

- x

## History

| Date | Reporter | Resolution | Time to Fix |
|------|----------|------------|--------------|
| 2026-01-01 | x | x | x |
EOF
DUP_IN_V3="$V3/Runbooks/Auth/$(basename "$DRAFT")"
cp "$DRAFT" "$DUP_IN_V3"
out="$(MEMORY_VAULT_ROOT="$V3" MMDC="$MMDC" PUPPETEER_CFG="$PUPPETEER_CFG" bash "$VALIDATE" "$DUP_IN_V3" 2>&1)"; rc=$?
if [[ $rc -eq 1 ]] && [[ "${out#*duplicado}" != "$out" ]]; then
  ok "a draft whose id is already used elsewhere in the vault fails check 3 (duplicado)"
else
  bad "a draft with a duplicate id must fail check 3" "got $rc: $out"
fi

# 3c. PII in the body -> check 9 FAIL. Injected value is synthetic
# (test@example.com-shaped, RFC 2606 .invalid domain), never real customer
# data -- see tests/fixtures/README.md for the project's sanitization rule.
V4="$(fresh_ephemeral_vault v4)"
mkdir -p "$V4/Runbooks/Auth"
PII_DRAFT="$V4/Runbooks/Auth/$(basename "$DRAFT")"
cp "$DRAFT" "$PII_DRAFT"
sd -- '## Prevention' '## Prevention

- Contact synthetic-canary-user@example.invalid for follow-up (canary-injected, not real)' "$PII_DRAFT"
out="$(MEMORY_VAULT_ROOT="$V4" MMDC="$MMDC" PUPPETEER_CFG="$PUPPETEER_CFG" bash "$VALIDATE" "$PII_DRAFT" 2>&1)"; rc=$?
if [[ $rc -eq 1 ]] && [[ "${out#*FAIL] 9 }" != "$out" ]]; then
  ok "a draft with an email address outside {{variables}} fails check 9 (PII)"
else
  bad "a draft with a body email address must fail check 9" "got $rc: $out"
fi

printf '\n%s canaries passed, %s failed\n' "$PASS_N" "$FAIL_N"
[[ $FAIL_N -eq 0 ]] || exit 1
