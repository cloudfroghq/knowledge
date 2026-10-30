#!/usr/bin/env bash
# Prints the next free RB-NNN id by scanning frontmatter ids in the vault (numeric ids only).
#
# Reads directly from the vault instead of a hardcoded constant, per plantilla-runbook.md
# section 6: the id a person gets today has to reflect the real vault, not a snapshot of it.
#
# Usage: next-runbook-id.sh
# Env:   MEMORY_VAULT_ROOT  the vault root (otherwise VAULT_PATH): a clone of the vault's own
#                           repository, since no vault lives in this checkout
set -euo pipefail

# shellcheck source=lib/guards.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/guards.sh"

vault="${MEMORY_VAULT_ROOT:-$(runbook_vault_root || true)}"
if [[ -z "$vault" ]]; then
  printf 'next-runbook-id.sh: no se resolvió la raíz del vault; exporta MEMORY_VAULT_ROOT\n' >&2
  exit 1
fi

if [[ ! -d "$vault/Runbooks" ]]; then
  printf 'next-runbook-id.sh: no existe %s/Runbooks\n' "$vault" >&2
  exit 1
fi

last="$(rg -o --no-filename '^id: RB-([0-9]{3})$' -r '$1' "$vault/Runbooks" -g '*.md' 2> /dev/null | sort -n | tail -1)"

# The `10#` prefix forces base-10 parsing. Without it, an id like RB-008 or RB-009 makes
# bash read "008"/"009" as an (invalid) octal literal and abort with "value too great for
# base" -- verified by hand: `bash -c 'echo $(( 008 + 1 ))'` fails on this machine's bash 3.2.
next=$(( 10#${last:-0} + 1 ))
printf 'RB-%03d\n' "$next"
