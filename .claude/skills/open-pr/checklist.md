# Checklist — open-pr

- [ ] Source branch confirmed (`git branch --show-current`); it is not a spike that never merges.
- [ ] The branch's commits are Conventional Commits, with no ticket prefix, atomic.
- [ ] `.claude/rules/git.md` consulted; merge destination confirmed for this change type, not assumed.
- [ ] `bkt pr list --state OPEN --json --jq '…select(.source.branch.name == "<branch>")'` run; no PR already open from this branch.
- [ ] PR body written from `.github/PULL_REQUEST_TEMPLATE.md` (Summary, Type, Tool contract impact, Methodology, Tests, Gates, Notes for reviewers), with no generic filler text.
- [ ] `git push -u origin <branch>` confirmed before creating the PR.
- [ ] `bkt pr create --target <destination> --title "…" --description "…"` run with an explicit `--target` (never omitted and never as a positional argument).
- [ ] PR URL linked to the ticket with `jira_create_remote_issue_link`.
- [ ] Transition `121` (In Code Review) applied with `jira_transition_issue`.
- [ ] `mem_save` completed with the PR URL and the destination used.
