#!/usr/bin/env bash
# Weekly consolidation bridge: export stable memories into the vault's
# memory folder, replicate them to memory-cloud, then commit and push that
# folder to the vault's own repository (the checkout VAULT_PATH, or the default
# vault of vaults.yaml, points at). The GitHub push webhook of that repository
# makes the knowledge server pull the vault and reindex incrementally, so
# nothing here calls the server directly.
#
# Flow: fast-forward the branch -> export -> cloud sync -> commit -> push.
#
# Environment (all optional):
#   MEMORY_PROJECT  memory project to export and replicate (default: knowledge)
#   BRIDGE_BRANCH   branch of the vault repository the server's clone tracks (default: main)
#   BRIDGE_DRY_RUN  1 exports and reports, but never fast-forwards, commits or pushes
#   MEMORY_BIN      memory binary (default: /opt/homebrew/bin/memory)
#
# `--dry-run` as an argument is equivalent to BRIDGE_DRY_RUN=1.
#
# Exit codes: 2 unknown argument, 3 wrong branch checked out in the vault
# repository, 4 branch cannot be fast-forwarded, 5 the vault is not a git
# checkout; any other failure propagates from the failing command.
#
# launchd starts this with a minimal PATH and no shell profile: every binary is
# referenced by absolute path, and git identity plus ssh keys resolve from
# $HOME (~/.gitconfig, ~/.ssh/config).
set -euo pipefail

export PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin

GIT=/usr/bin/git
UV=/opt/homebrew/bin/uv
MEMORY_BIN="${MEMORY_BIN:-/opt/homebrew/bin/memory}"
MEMORY_PROJECT="${MEMORY_PROJECT:-knowledge}"
BRIDGE_BRANCH="${BRIDGE_BRANCH:-main}"
BRIDGE_DRY_RUN="${BRIDGE_DRY_RUN:-0}"

for arg in "$@"; do
  case "$arg" in
    --dry-run) BRIDGE_DRY_RUN=1 ;;
    *)
      echo "bridge: unknown argument '$arg' (only --dry-run is accepted)" >&2
      exit 2
      ;;
  esac
done

REPO="$(cd "$(dirname "$0")/.." && "$GIT" rev-parse --show-toplevel)"
cd "$REPO"

# Same vault and memory root the export computes (VAULT_PATH or the default
# vault of vaults.yaml, plus memory.vault_folder), so the commit scope can
# never drift from where the notes are written.
VAULT_DIR="$("$UV" run --quiet python -c 'from knowledge import config; print(config.VAULT_PATH)')"
MEMORY_ROOT="$("$UV" run --quiet python -c 'from knowledge import config; print(config.VAULT_PATH / config.MEMORY_FOLDER)')"

# The vault lives in its own repository: every git command below runs there,
# never in this one.
VAULT_REPO="$("$GIT" -C "$VAULT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$VAULT_REPO" ]; then
  echo "bridge: the vault at '$VAULT_DIR' is not a git checkout; nothing exported, committed or pushed" >&2
  exit 5
fi
VGIT=("$GIT" -C "$VAULT_REPO")

# The push has to land on the branch the server clones; exporting onto any
# other checkout would strand memory notes on a feature branch.
branch="$("${VGIT[@]}" symbolic-ref --short -q HEAD || true)"
if [ "$branch" != "$BRIDGE_BRANCH" ]; then
  echo "bridge: the vault repository has '${branch:-detached HEAD}' checked out, expected '$BRIDGE_BRANCH'; nothing exported, committed or pushed" >&2
  exit 3
fi

# Incremental export state. memory bumps its timestamp on every run, so the
# file is ignored when deciding whether there is anything worth committing.
STATE_FILE="$MEMORY_ROOT/memory/.memory-sync-state.json"

if [ "$BRIDGE_DRY_RUN" = 1 ]; then
  "${VGIT[@]}" fetch --quiet origin "$BRIDGE_BRANCH"
  behind="$("${VGIT[@]}" rev-list --count "HEAD..origin/$BRIDGE_BRANCH")"
  ahead="$("${VGIT[@]}" rev-list --count "origin/$BRIDGE_BRANCH..HEAD")"
  echo "bridge: dry run; $BRIDGE_BRANCH is $behind behind and $ahead ahead of origin/$BRIDGE_BRANCH (not fast-forwarding)"
else
  # Bring the branch up to date first: the export is incremental over the
  # committed state, and a push from a stale branch is rejected anyway.
  "${VGIT[@]}" pull --ff-only --quiet origin "$BRIDGE_BRANCH" || {
    echo "bridge: cannot fast-forward $BRIDGE_BRANCH onto origin/$BRIDGE_BRANCH in $VAULT_REPO; reconcile the branch by hand" >&2
    exit 4
  }
fi

"$UV" run --quiet python scripts/export_memories.py --project "$MEMORY_PROJECT"

# Replicate local memories to the remote memory-cloud (Dokploy Memory stack).
# Token read at runtime from infra/.env.server — never embedded here. A cloud
# failure must not block the vault export, so it only warns.
if [ -f "$REPO/infra/.env.server" ]; then
  MEMORY_CLOUD_TOKEN=$(/usr/bin/awk -F= '/^SERVER_MEMORY_CLOUD_TOKEN=/{print $2}' "$REPO/infra/.env.server") \
    "$MEMORY_BIN" sync --cloud --project "$MEMORY_PROJECT" \
    || echo "bridge: memory cloud sync failed for project '$MEMORY_PROJECT' (continuing)" >&2
fi

if [ "$BRIDGE_DRY_RUN" = 1 ]; then
  echo "bridge: dry run; working tree status under $MEMORY_ROOT:"
  "${VGIT[@]}" status --porcelain -- "$MEMORY_ROOT"
  echo "bridge: dry run; push probe (nothing is committed; a rejection for being behind is expected):"
  "${VGIT[@]}" push --dry-run origin "$BRIDGE_BRANCH" \
    || echo "bridge: dry run; push probe exited $? (see above)" >&2
  exit 0
fi

# Commit only the memory folder. New, modified and deleted notes are staged,
# but a run whose only change is the export timestamp is skipped: that push
# would fire the reindex webhook for nothing.
note_changes="$("${VGIT[@]}" status --porcelain -- "$MEMORY_ROOT" ":(exclude)$STATE_FILE")"
if [ -z "$note_changes" ]; then
  echo "bridge: no note changes under $MEMORY_ROOT; nothing to commit"
  exit 0
fi

"${VGIT[@]}" add -A -- "$MEMORY_ROOT"
if "${VGIT[@]}" diff --cached --quiet -- "$MEMORY_ROOT"; then
  echo "bridge: nothing staged under $MEMORY_ROOT; nothing to commit"
  exit 0
fi

# The pathspec keeps unrelated staged changes out of this commit.
"${VGIT[@]}" commit --quiet -m "chore(vault): sync memory bridge export" -- "$MEMORY_ROOT"
"${VGIT[@]}" push --quiet origin "$BRIDGE_BRANCH"
echo "bridge: pushed $("${VGIT[@]}" rev-parse --short HEAD) to origin/$BRIDGE_BRANCH; the push webhook reindexes the server"
