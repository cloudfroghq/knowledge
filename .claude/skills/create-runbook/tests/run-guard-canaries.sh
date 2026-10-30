#!/usr/bin/env bash
# run-guard-canaries.sh — canary battery for scripts/lib/guards.sh, the guard
# functions instantiate-runbook.sh and run-timed-rehearsal.sh both rely on to
# never write inside the real vault and to never let the 15-minute budget
# assertion be a no-op.
#
# These are the exact functions the real scripts call (`source`d, not
# reimplemented), so a canary failure here means the real scripts would behave
# the same way -- and each assertion below has a branch that is forced to fail
# first, so the "must pass" branch is not the only one ever exercised.
#
# Bash-3.2-safe: no arrays, plain counters. Usage: bash run-guard-canaries.sh
set -u

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SELF_DIR/.." && pwd)"
# shellcheck source=../scripts/lib/guards.sh
source "$SKILL_DIR/scripts/lib/guards.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS_N=0
FAIL_N=0
ok()  { PASS_N=$((PASS_N + 1)); printf '[ok]   %s\n' "$1"; }
bad() { FAIL_N=$((FAIL_N + 1)); printf '[FAIL] %s -- %s\n' "$1" "$2"; }

printf 'bash: %s\n\n' "$BASH_VERSION"

# --------------------------------------------------------------------------- #
# guard_dest_outside_vault
# --------------------------------------------------------------------------- #
FAKE_VAULT="$TMP/fake-vault"
mkdir -p "$FAKE_VAULT/Runbooks"

OUTSIDE="$TMP/outside-dir"
mkdir -p "$OUTSIDE"
if guard_dest_outside_vault "$OUTSIDE" "$FAKE_VAULT" > /dev/null 2>&1; then
  ok "a directory outside the vault is accepted (exit 0)"
else
  bad "a directory outside the vault must be accepted" "got exit $?"
fi

INSIDE="$FAKE_VAULT/Runbooks/would-be-write"
mkdir -p "$INSIDE"
out="$(guard_dest_outside_vault "$INSIDE" "$FAKE_VAULT" 2>&1)"; rc=$?
if [[ $rc -eq 3 ]] && [[ "${out#*inside the real vault}" != "$out" ]]; then
  ok "a directory inside the vault is refused (exit 3, named in the message)"
else
  bad "a directory inside the vault must be refused with exit 3" "got exit $rc: $out"
fi

if guard_dest_outside_vault "$FAKE_VAULT" "$FAKE_VAULT" > /dev/null 2>&1; then
  bad "the vault root itself must be refused, not accepted" "got exit 0"
else
  ok "the vault root itself is refused, not just its children"
fi

# A relative path and a trailing slash must not slip past the check -- both
# get resolved to the same absolute, symlink-free form before comparing.
( cd "$FAKE_VAULT" && guard_dest_outside_vault "Runbooks/../Runbooks" "$FAKE_VAULT" ) > /dev/null 2>&1
if [[ $? -eq 3 ]]; then
  ok "a relative path that resolves inside the vault is still refused"
else
  bad "a relative path resolving inside the vault must still be refused" "got exit $?"
fi

# --------------------------------------------------------------------------- #
# guard_under_budget -- the function behind the rehearsal's 15-minute
# assertion. This is what makes that assertion falsifiable without an agent
# actually waiting 15 minutes: both branches are exercised directly here.
# --------------------------------------------------------------------------- #
if guard_under_budget 300 900 > /dev/null 2>&1; then
  ok "300s elapsed against a 900s budget passes"
else
  bad "300s elapsed against a 900s budget must pass" "got exit $?"
fi

out="$(guard_under_budget 900 900 2>&1)"; rc=$?
if [[ $rc -eq 4 ]] && [[ "${out#*elapsed 900s}" != "$out" ]]; then
  ok "900s elapsed against a 900s budget fails (exit 4, elapsed named)"
else
  bad "900s elapsed against a 900s budget must fail with exit 4" "got exit $rc: $out"
fi

out="$(guard_under_budget 1200 900 2>&1)"; rc=$?
if [[ $rc -eq 4 ]]; then
  ok "1200s elapsed against a 900s budget fails"
else
  bad "1200s elapsed against a 900s budget must fail" "got exit $rc: $out"
fi

# --------------------------------------------------------------------------- #
# guard_pattern_known
# --------------------------------------------------------------------------- #
mkdir -p "$TMP/patterns"
printf -- '---\npattern: demo\n---\n' > "$TMP/patterns/demo.md"

if guard_pattern_known "demo" "$TMP/patterns" > /dev/null 2>&1; then
  ok "a pattern with a defaults file on disk is accepted"
else
  bad "a pattern with a defaults file on disk must be accepted" "got exit $?"
fi

out="$(guard_pattern_known "not-a-real-pattern" "$TMP/patterns" 2>&1)"; rc=$?
if [[ $rc -eq 5 ]] && [[ "${out#*no defaults file}" != "$out" ]]; then
  ok "an unknown pattern is refused (exit 5, named in the message)"
else
  bad "an unknown pattern must be refused with exit 5" "got exit $rc: $out"
fi

printf '\n%s canaries passed, %s failed\n' "$PASS_N" "$FAIL_N"
[[ $FAIL_N -eq 0 ]] || exit 1
