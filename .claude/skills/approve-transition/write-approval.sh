#!/usr/bin/env bash
# .claude/skills/approve-transition/write-approval.sh — issues the human
# approval token for a Jira transition that guard-jira-transition.sh blocked
# (ADR-031). Only a person runs this; the model never invokes this skill.
#
# Usage: write-approval.sh <ISSUE_KEY> <TRANSITION_ID>
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: write-approval.sh <ISSUE_KEY> <TRANSITION_ID>" >&2
  exit 1
fi

issue_key="$1"
transition_id="$2"

case "$transition_id" in
  61|111|121)
    echo "transition $transition_id is already unattended-safe; no approval needed" >&2
    exit 1
    ;;
esac

approvals_dir="${CD_APPROVALS_DIR:-$HOME/.knowledge/approvals}"
mkdir -p "$approvals_dir"
token_file="$approvals_dir/$issue_key-$transition_id.json"

printf '{"issue_key":"%s","transition_id":"%s","approved_by":"%s","approved_at":"%s","ttl_minutes":30}\n' \
  "$issue_key" "$transition_id" "${USER:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$token_file"

echo "token written: $token_file (consume-on-use, 30 min TTL)"
echo "guard-jira-transition.sh checks CD_JIRA_APPROVED_TRANSITION in this session, so also run:"
echo "  export CD_JIRA_APPROVED_TRANSITION=$transition_id"
