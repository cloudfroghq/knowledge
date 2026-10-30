# guards.sh — shared guard functions for create-runbook's scripts and its test battery.
#
# Sourced only, never executed: `source lib/guards.sh` from instantiate-runbook.sh,
# run-timed-rehearsal.sh and the canary batteries under tests/. Keeping the checks
# here means the canaries exercise the exact same code the real scripts run, not a
# reimplementation of it.
#
# Bash-3.2-safe: no arrays, no associative arrays, no ${var,,}, no `local -n`.

# --------------------------------------------------------------------------- #
# Checkout resolution
#
# This package is deployed into the knowledge checkout, whose directory
# name is not guaranteed, and it runs next to an ai-architecture checkout whose
# name is not guaranteed either. Neither may be written as a path here: a path
# baked into a script decides by accident whether a run works on another
# machine. Everything below is resolved from this file's own position plus a
# marker that identifies the checkout by its contents.
#
# Each resolver prints the path and returns 0, or prints nothing and returns 1.
# The caller decides whether an empty result is a SKIP (nothing was verified)
# or a FAIL; it is never silently treated as success.
# --------------------------------------------------------------------------- #

# The vault lives in its own repository, never in this checkout: it is the
# directory VAULT_PATH names (a clone of the vault repository). Callers try
# MEMORY_VAULT_ROOT first; without either there is nothing to resolve.
runbook_vault_root() {
  if [ -n "${VAULT_PATH:-}" ] && [ -d "$VAULT_PATH" ]; then
    (cd "$VAULT_PATH" && pwd -P)
    return 0
  fi
  return 1
}

# The checkout this package is deployed into: the nearest ancestor carrying the
# server's config.json next to its src/knowledge/. That file is where a deployment
# declares what it knows -- among other things which services its runbooks may
# name -- so anything that has to read a deployment's own settings resolves the
# directory here rather than guessing a level count from its own position.
runbook_project_root() {
  local d
  d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  while [ "$d" != "/" ]; do
    if [ -f "$d/config.json" ] && [ -d "$d/src/knowledge" ]; then printf '%s' "$d"; return 0; fi
    d="$(dirname "$d")"
  done
  return 1
}

# The ai-architecture checkout: the one that carries ai-platform-plan/. It is
# either an ancestor of this file (the package sitting in the plan tree) or a
# sibling of the checkout that holds it (the deployed package).
runbook_arch_root() {
  local d sib
  d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  while [ "$d" != "/" ]; do
    if [ -d "$d/ai-platform-plan" ]; then printf '%s' "$d"; return 0; fi
    d="$(dirname "$d")"
  done
  d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  while [ "$d" != "/" ]; do
    if [ -d "$d/contexts" ] || [ -d "$d/vault" ]; then
      for sib in "$(dirname "$d")"/*/; do
        sib="${sib%/}"
        [ -d "$sib/ai-platform-plan" ] && { printf '%s' "$sib"; return 0; }
      done
      return 1
    fi
    d="$(dirname "$d")"
  done
  return 1
}

# The one real vault on this machine, or empty when this package is not
# installed in a checkout that has one. Callers must pass this constant (never a
# value derived from MEMORY_VAULT_ROOT, which a rehearsal legitimately points at
# an ephemeral copy) so the guard always protects the actual curated vault,
# regardless of what any script's own vault-root override is set to.
REAL_VAULT_ROOT_DEFAULT="$(runbook_vault_root || true)"

# guard_dest_outside_vault <dest_dir> <real_vault_root>
# Exit 0 when dest_dir is NOT inside (or equal to) real_vault_root. Exit 3 and
# print an explicit error to stderr otherwise. Resolves both paths to their
# absolute, symlink-free form first (via `cd ... && pwd -P`) so a relative path,
# a trailing slash, or a symlinked ancestor cannot slip past a naive string
# comparison — verified by hand: without `pwd -P`, `vault/../vault`
# and a same-named sibling directory both false-negative on a plain `==`.
guard_dest_outside_vault() {
  local dest="$1" vault="$2" dest_abs vault_abs
  # An empty vault root would make the case below match "/*" and reject every
  # absolute destination for the wrong reason. Refuse explicitly instead: the
  # guard cannot protect a vault it could not locate.
  if [ -z "$vault" ]; then
    printf 'guard_dest_outside_vault: no se resolvió la raíz del vault; exporta MEMORY_VAULT_ROOT o ejecuta desde el checkout de knowledge\n' >&2
    return 3
  fi
  dest_abs="$(cd "$dest" 2> /dev/null && pwd -P)" || dest_abs="$dest"
  vault_abs="$(cd "$vault" 2> /dev/null && pwd -P)" || vault_abs="$vault"
  case "$dest_abs" in
    "$vault_abs" | "$vault_abs"/*)
      printf 'guard_dest_outside_vault: "%s" resolves inside the real vault "%s" -- refusing to write\n' "$dest_abs" "$vault_abs" >&2
      return 3
      ;;
  esac
  return 0
}

# guard_under_budget <elapsed_seconds> <budget_seconds>
# Exit 0 when elapsed < budget. Exit 4 with a message on stderr otherwise.
# Both arguments must be plain non-negative integers (whole seconds).
guard_under_budget() {
  local elapsed="$1" budget="$2"
  if (( elapsed >= budget )); then
    printf 'guard_under_budget: elapsed %ss >= budget %ss\n' "$elapsed" "$budget" >&2
    return 4
  fi
  return 0
}

# guard_pattern_known <pattern> <patterns_dir>
# Exit 0 when <patterns_dir>/<pattern>.md exists (the pattern has defaults on
# file). Exit 5 with a message on stderr otherwise -- this is what stops an
# unknown or misspelled pattern before it ever reaches the template.
guard_pattern_known() {
  local pattern="$1" dir="$2"
  if [[ -f "$dir/$pattern.md" ]]; then
    return 0
  fi
  printf 'guard_pattern_known: no defaults file for pattern "%s" in %s -- see ADR-032 for the enum\n' "$pattern" "$dir" >&2
  return 5
}

# frontmatter_json_of <file>
# Prints the YAML frontmatter (between the first two "---" lines) of <file> as
# JSON on stdout, or nothing if it does not parse. Small and deliberately
# independent from validate-runbook.sh's own copy: that script is a driver with
# side effects at the bottom and is not meant to be sourced.
frontmatter_json_of() {
  local file="$1"
  awk '
    NR == 1 && $0 == "---" { infm = 1; next }
    infm && $0 == "---" { exit }
    infm { print }
  ' "$file" | yq -o=json -p=yaml '.' - 2> /dev/null
}
