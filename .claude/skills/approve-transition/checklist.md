# Checklist — approve-transition

- [ ] The transition was decided by a person, outside the agent.
- [ ] The transition id is not `61`, `111` or `121` (those do not need this token).
- [ ] `write-approval.sh <ticket-key> <transition-id>` run; token created in `$CD_APPROVALS_DIR` (or `~/.knowledge/approvals` by default).
- [ ] `CD_JIRA_APPROVED_TRANSITION=<transition-id>` exported in the session before invoking `jira_transition_issue`.
- [ ] `jira_transition_issue` run; `guard-jira-transition.sh` let the call through.
- [ ] `mem_save` completed with who approved, when, ticket and transition.
