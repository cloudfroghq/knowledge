#!/usr/bin/env bash
# SessionStart hook of the knowledge plugin.
#
# Runs `cloudfrog-knowledge doctor --brief` with the same package spec and the
# same plugin options the MCP server starts with. Silent on the happy path:
# nothing reaches the session context. When the doctor finds a problem, when
# a served vault declares context packages this machine has not linked yet
# (`init pending`, still exit status 0), or when uvx is missing, it prints one
# line of at most 200 bytes, the bound `doctor --brief` keeps. The bound
# counts the line, not its line break, so stdout is at most 201 bytes.
#
# Two cases stay quiet so a machine without the package does not repeat itself:
#
# - A doctor that does not answer within the budget (a cold uv cache downloads
#   the package) is silent: the next session reports instead.
# - When uvx fails without any doctor output (no network, or a tag the remote
#   does not have), the "server package failed" line is printed once per day
#   and per package spec. The marker is a one-line file under
#   ${XDG_CACHE_HOME:-$HOME/.cache}/knowledge/. If the marker cannot be
#   written, the line is printed on every start. The server shows as failed in
#   /mcp in the meantime. Retrying with `uvx --offline` would not help: uv 0.10
#   still fetches a git tag with that flag, so the cache alone cannot answer.
#
# When the doctor is happy, the hook also starts `cloudfrog-knowledge jira sync
# --auto` in the background, detached from the hook: its own process group,
# stdin, stdout and stderr on /dev/null, so neither the hook's timeout nor the
# session waits on it or sees its output. That run syncs only the vaults that
# declare the jira extension, have their credentials exported and are past
# their sync.throttle_minutes; anything else exits at once. It writes the
# vault's jira_sync.json under the index directory and the tasks of the memory
# project. KNOWLEDGE_JIRA_AUTOSYNC=0 turns it off.
#
# The only thing the hook itself writes to disk is the marker above.
#
# The interpreter is bash, found on PATH: the budget below needs job control
# without a terminal (`set -m` giving the doctor its own process group, and
# `kill -- -PGID`). dash, /bin/sh on Debian and Ubuntu, turns job control off
# without a tty and rejects the negative pid, so under it the budget never
# fires and the hook waits for the doctor until its own timeout.

set -u

BUDGET_SECONDS=${KNOWLEDGE_DOCTOR_BUDGET:-8}
MAX_BYTES=200

say() {
  # Byte-bounded output: printf precision counts bytes in the C locale.
  LC_ALL=C printf '%.*s\n' "$MAX_BYTES" "$1"
}

root=${CLAUDE_PLUGIN_ROOT:-$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)}
uvx=${UVX_BIN:-uvx}

if ! command -v "$uvx" >/dev/null 2>&1; then
  say "knowledge: uvx not found; install uv or set UVX_BIN to its absolute path"
  exit 0
fi

# The package spec lives only in .mcp.json, so the hook can never check a
# different release than the one the server runs.
spec=
while IFS= read -r line || [ -n "$line" ]; do
  case $line in
    *'"git+'*)
      rest=${line#*\"git+}
      spec="git+${rest%%\"*}"
      break
      ;;
  esac
done <"$root/.mcp.json"
[ -n "$spec" ] || exit 0

# Same environment the server receives from userConfig: an empty value reads
# as unset, so the doctor falls back to ~/.config/knowledge/vaults.yaml.
KNOWLEDGE_VAULTS=${CLAUDE_PLUGIN_OPTION_VAULTS_CONFIG:-}
KNOWLEDGE_EMBEDDINGS_PROVIDER=${CLAUDE_PLUGIN_OPTION_EMBEDDINGS_PROVIDER:-}
export KNOWLEDGE_VAULTS KNOWLEDGE_EMBEDDINGS_PROVIDER

run_doctor() {
  # Job control puts the doctor in its own process group, so the budget kills
  # uvx and the interpreter it spawned together, and no orphan keeps the
  # output pipe open.
  set -m
  "$uvx" --quiet --managed-python --python 3.12 --from "$spec" cloudfrog-knowledge doctor --brief 2>/dev/null &
  pid=$!
  (sleep "$BUDGET_SECONDS" && kill -TERM -- "-$pid" 2>/dev/null) >/dev/null 2>&1 &
  watcher=$!
  wait "$pid"
  status=$?
  kill "$watcher" 2>/dev/null
  return "$status"
}

out=$(run_doctor)
status=$?

# True when today's report of a failed package was not printed yet. The marker
# holds `<date> <spec>`: a new day, or a new release of the plugin, reports once
# more. No cache directory (no HOME) or an unwritable one never silences the line.
first_report_today() {
  cache_home=${XDG_CACHE_HOME:-}
  if [ -z "$cache_home" ] && [ -n "${HOME:-}" ]; then
    cache_home=$HOME/.cache
  fi
  [ -n "$cache_home" ] || return 0

  marker=$cache_home/knowledge/session-start-package-failed
  stamp="$(date +%Y%m%d) $spec"
  seen=
  if [ -r "$marker" ]; then
    IFS= read -r seen <"$marker" || :
  fi
  [ "$seen" != "$stamp" ] || return 1

  { mkdir -p "$cache_home/knowledge" && printf '%s\n' "$stamp" >"$marker"; } 2>/dev/null || :
  return 0
}

# Fire and forget: a subshell starts the sync as a job in its own process
# group and exits, so the job is reparented and the hook returns at once.
start_jira_sync() {
  [ "${KNOWLEDGE_JIRA_AUTOSYNC:-1}" != 0 ] || return 0
  (
    set -m
    nohup "$uvx" --quiet --managed-python --python 3.12 --from "$spec" \
      cloudfrog-knowledge jira sync --auto </dev/null >/dev/null 2>&1 &
  ) </dev/null >/dev/null 2>&1
}

case $status in
  0)
    # A healthy doctor that still has something to say (a vault whose context
    # packages this machine has not linked yet) prints a line that is not the
    # "ok" one; that line reaches the session, the "ok" one never does.
    case $out in
      "" | "knowledge doctor: ok"*) ;;
      *) say "$out" ;;
    esac
    start_jira_sync
    ;;
  # Killed by the budget: the cache is still cold. Stay silent.
  137 | 143) ;;
  *)
    if [ -n "$out" ]; then
      say "$out"
    elif first_report_today; then
      say "knowledge: server package failed (uvx rc $status); diagnose: uvx --managed-python --python 3.12 --from '$spec' cloudfrog-knowledge doctor"
    fi
    ;;
esac
exit 0
