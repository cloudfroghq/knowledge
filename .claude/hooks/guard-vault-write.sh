#!/usr/bin/env bash
# .claude/hooks/guard-vault-write.sh — PreToolUse on Write|Edit.
#
# Vaults are curated content, not code, and none of them lives in this
# repository: each one has its own repository (one per organization that owns
# the content), and
# the owner keeps their clones under $HOME/Projects/Vaults/<slug>. A vault is
# edited by human PR, never by the agent (D-11). The single exception is
# `90 - Memory/`, the only folder the automatic bridge writes to.
#
# KNOWLEDGE_VAULTS_ROOT overrides the clone root for a machine that keeps them
# elsewhere. Limit: this hook sees file_path only (Write|Edit). A Bash command
# that writes under a vault (for example `vault-lint --fix` run on a clone) is
# not caught here; the server's own write fence (E_WRITE_FENCED) and the rule
# of proposing changes as patches outside the vault cover that path.
set -euo pipefail

path="$(jq -r '.tool_input.file_path // empty' 2>/dev/null || true)"
[ -z "$path" ] && exit 0

root="${KNOWLEDGE_VAULTS_ROOT:-${HOME:-}/Projects/Vaults}"
[ -z "$root" ] && exit 0

case "$path" in
  "$root"/*/90\ -\ Memory/*) exit 0 ;;
  "$root"/*)
    echo "guard-vault-write: ${path} is curated vault content; it is edited by human PR on the vault's own repository, never by the agent (D-11). Only '<vault>/90 - Memory/' accepts automatic writes — propose the change as a patch outside the vault, or save the finding to memory with mem_save." >&2
    exit 2 ;;
esac
exit 0
