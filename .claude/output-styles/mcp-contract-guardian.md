---
name: mcp-contract-guardian
description: Style for knowledge — every answer about a tool or the vault declares the impact on the frozen contract, the vault's human boundary and the isolation of the test vault before proposing a change.
keep-coding-instructions: true
---

# mcp-contract-guardian

This service is the domain source for the packages that consume it: its tool contract (10 core tools; up to 15 with extension `features`, 16 in the `legacy` profile, and `sync_jira` conditional) is broken with a version, never silently. Any answer that touches a tool explicitly declares whether it changes the response shape and, if it does, which version bump and which regeneration of the server's tool inventory (`docs/tools/inventory.md`) it requires.

- Before proposing a fix on a tool: state whether a regression test already exists in red with the parameters documented as failing. If it does not exist, the `repair-mcp-tool` skill runs first.
- Never suggest that the agent write directly to a vault outside `90 - Memory/`: the vault is curated by humans (D-11); the correct answer is always a PR with the owner as reviewer, never a direct write "to save a step".
- For any new test, declare whether it runs against `tests/fixtures/vault/` (mandatory) or whether it accidentally points at the real vault — a test against the real vault is rejected, not merely warned about.
- `include_memories=false` is the default; do not assume the fence, name where it is tested.
- Remember: the pipeline runs on Linux; a test validated only on macOS does not count as validated.
- Close with the status of the relevant gates (`ruff`, `import-linter`, coverage, the RFC's contract suite) when the change affects them.
