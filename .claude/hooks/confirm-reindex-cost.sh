#!/usr/bin/env bash
# .claude/hooks/confirm-reindex-cost.sh — PreToolUse on Bash(*reindex*).
#
# A reindex recomputes embeddings for the whole vault: it costs money on the
# embedding API and it takes minutes. It is a mutating operation that needs a
# write-scoped token, so it is never a casual command.
#
# Authorise one run with: export CD_REINDEX_CONFIRMED=1
set -euo pipefail

cmd="$(jq -r '.tool_input.command // empty' 2>/dev/null || true)"
[ -z "$cmd" ] && exit 0

case "$cmd" in
  *--dry-run*|*--help*|*-h) exit 0 ;;
esac

if [ "${CD_REINDEX_CONFIRMED:-}" = "1" ]; then
  echo "confirm-reindex-cost: authorised by CD_REINDEX_CONFIRMED=1" >&2
  exit 0
fi

echo "confirm-reindex-cost: a reindex recomputes embeddings for the whole vault — it bills the embedding API and takes minutes. Confirm the cost with 'export CD_REINDEX_CONFIRMED=1' for this session, or add --dry-run to see the plan without paying for it." >&2
exit 2
