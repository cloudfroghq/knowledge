---
description: Resolves an EXMP bug ticket in knowledge end to end, with a regression test and a PR ready for review.
allowed-tools: mcp__mcp-atlassian__jira_get_issue, mcp__mcp-atlassian__jira_add_comment, mcp__mcp-atlassian__jira_transition_issue, Bash(git *), Bash(gh pr create *), Bash(uv run pytest *), Edit, Write, Read
argument-hint: <EXMP-NNNN>
---

# fix-issue

Resolve ticket $1 end to end.

1. `mcp__mcp-atlassian__jira_get_issue` on $1 to get the symptom, the affected tool or component, and the acceptance criteria.
2. Create the branch `fix/$1` from an up-to-date `main`.
3. If the ticket is about an MCP tool, run the `repair-mcp-tool` skill instead of continuing this direct flow.
4. Write the regression test first and confirm it fails (red) (`uv run pytest -k <case>`).
5. Implement the minimal fix that makes the test pass.
6. `gh pr create --base main --title "fix: <summary>" --body "<details, including $1>"`.
7. `mcp__mcp-atlassian__jira_add_comment` with the technical summary of the fix and the PR link.
8. `mcp__mcp-atlassian__jira_transition_issue` to "In Code Review".

Nothing is written to any vault outside `90 - Memory/` as part of this command: a change to vault content goes through a human PR (D-11), never through this flow.
