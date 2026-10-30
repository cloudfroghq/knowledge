#!/usr/bin/env bash
# .claude/hooks/guard-jira-transition.sh — PreToolUse on jira_transition_issue:
# only reversible forward transitions run unattended.
#
# 61 = in Analysis, 111 = In Develop, 121 = In Code Review. A terminal or
# irreversible id needs a person (ADR-031): export
# CD_JIRA_APPROVED_TRANSITION=<id> to authorise one for this session.
#
# In the PreToolUse contract only exit 2 blocks the call; every other non-zero
# status is reported as a non-blocking error and the tool runs anyway. So every
# path out of this script that is not an explicit allow must end in exit 2,
# including the paths where the script itself could not do its job:
#
#   - jq missing            -> used to exit 127 -> the transition went through
#   - payload not JSON      -> used to exit 5   -> the transition went through
#   - transition_id absent  -> matched the unset CD_JIRA_APPROVED_TRANSITION
#                              ("" = "") and exited 0 -> allowed, unattended
#
# The last one is the sharpest: with no approval token in the environment, a
# payload carrying no transition id was waved through.
#
# What this hook still CANNOT do: classify by name. The tool input carries
# issue_key and transition_id and nothing else, and the ADR-031 id table only
# describes the workflow of issue type "Issue productivo" — on a "Task" the
# same numbers name other statuses. Closing that gap is the job of
# `resolve-ticket`'s scripts/check-transition.sh, which is given the name from
# jira_get_transitions before the call reaches this hook.
set -uo pipefail

deny() {
  echo "guard-jira-transition: $1 — ADR-031" >&2
  exit 2
}

if ! command -v jq > /dev/null 2>&1; then
  deny "jq is not on PATH, so this guard cannot read the tool input; a guard that cannot look does not approve"
fi

# Read stdin with a builtin: `cat` would be a second external dependency, and
# every dependency this hook does not verify is a way for it to fail open.
payload=""
IFS= read -r -d '' payload || true

if ! transition="$(printf '%s' "$payload" | jq -r '.tool_input.transition_id // ""' 2>/dev/null)"; then
  deny "the tool input did not parse as JSON, so the transition id could not be read"
fi

if [ -z "$transition" ]; then
  deny "the tool input carries no transition_id; an unidentified transition is never unattended"
fi

case "$transition" in
  61 | 111 | 121) exit 0 ;;
esac

approved="${CD_JIRA_APPROVED_TRANSITION:-}"
if [ -n "$approved" ] && [ "$approved" = "$transition" ]; then
  exit 0
fi

deny "transition $transition is terminal or irreversible; a person must approve it (export CD_JIRA_APPROVED_TRANSITION=$transition for this session)"
