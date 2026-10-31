---
name: open-pr
description: Opens the current branch's pull request with `bkt`, builds the description from the repo's template, links the Jira ticket and moves the transition to In Code Review.
when_to_use: when the work on a feature, fix, hotfix or refactor branch is ready for review and that branch does not have an open PR yet.
allowed-tools: ["Read", "Write(/tmp/pr-body-*.md)", "Bash(git status *)", "Bash(git log *)", "Bash(git branch --show-current)", "Bash(git push *)", "Bash(bkt pr create *)", "Bash(bkt pr list *)", "mcp__mcp-atlassian__jira_get_issue", "mcp__mcp-atlassian__jira_create_remote_issue_link", "mcp__mcp-atlassian__jira_transition_issue", "mcp__plugin_memory_memory__mem_save"]
disable-model-invocation: false
user-invocable: true
argument-hint: "[ticket-key]"
---

# open-pr

Opens the Bitbucket pull request for the current branch with `bkt`, builds its description from the repo's template, links the corresponding Jira ticket and leaves the ticket in `In Code Review`.

## Steps

1. `git branch --show-current` and `git log` to confirm the source branch: that it is of a type that gets merged (not a `chore/spike-*`, which is never merged) and that its commits are Conventional Commits, with no ticket prefix and atomic.
2. Open `.claude/rules/git.md` and find, in the "Branch patterns and merge destinations" table, the row whose "Work type" matches the change made on this branch. Use the value in its "Merge destination" column as the real destination for `bkt pr create` — never assume `master` by default: some repos maintain several series in parallel, where the destination depends on which series the change affects, and some change types have as destination the parent branch of a composite feature, not the repo's default.
3. `bkt pr list --state OPEN --limit 50 --json --jq '.pull_requests[] | select(.source.branch.name == "<current-branch>")'` to confirm that no open PR from this branch exists yet — `bkt pr list` does not filter by source branch, the filter is applied to its JSON output. If one already exists, a second one is not created.
4. Compose the PR body: follow the sections of `.github/PULL_REQUEST_TEMPLATE.md` as they are (and, if the repo does not have it materialized yet, use the same structure) — Summary, Type, Tool contract impact, Methodology, Tests, Gates, Notes for reviewers — with real content from this branch, with no generic filler text. Save the result to a temporary file, for example `/tmp/pr-body-<ticket-key>.md`.
5. The PR title is the message of the branch's first conventional commit.
6. `git push -u origin <current-branch>`.
7. `bkt pr create --target <destination> --title "<title>" --description "$(bat -p /tmp/pr-body-<ticket-key>.md)"` — the destination always goes explicitly in `--target` (never as a positional argument): omitting it makes `bkt` use the remote's default branch, which is not always the correct destination. The source branch is the current one by default and can be set with `--source`. `bkt pr create` has no `--body-file`: the long description goes in through command substitution.
8. `jira_create_remote_issue_link` with the PR URL that `bkt pr create` returned, on the ticket given in the argument or found in the branch name.
9. `jira_transition_issue` with `transition_id: "121"` (In Code Review) — allowed without human approval by `guard-jira-transition.sh` (ADR-031). Any other transition is outside this skill.
10. `mem_save` with topic key `jira-implementer/<ticket-key>/pr` (inside a `jira-implementer` flow) or `project/<slug>/pr/<ticket-key>` (manual use), with the PR URL and the destination used.

## When NOT to use it

- If the branch is a spike (`chore/spike-*`): those are never merged; they are discarded after saving the findings in memory.
- If the real destination is the parent branch of a composite feature (subfeature): the PR is still opened with this skill, but the destination read in step 2 is that parent branch, never the repo's default.
- If the transition that applies is not `121` (the ticket already moved to a later stage): use `jira_transition_issue` directly or, if the id is terminal or irreversible, the `approve-transition` skill.
