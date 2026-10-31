---
name: repair-mcp-tool
description: Fixes a broken MCP tool of the knowledge server without breaking the ones that work.
when_to_use: when a tool of the knowledge server is documented as broken and has (or needs) a failing regression test with the exact input that fails.
allowed-tools: Read, Write, Edit, Bash(uv run *), Bash(graphify *), mcp__plugin_knowledge_knowledge__get_document, mcp__plugin_memory_memory__mem_save
model: sonnet
effort: high
disable-model-invocation: false
user-invocable: true
argument-hint: <tool-name>
---

# repair-mcp-tool

Fix the tool $1 without breaking the rest of the contract. Work in a checkout of the server repository.

## Steps

1. Reproduce the failure with the exact documented parameters and save the literal error output.
2. Write the `pytest` test that reproduces that failure, against the repository's test vault (never against a real vault), and see it fail.
3. `graphify explain "<function>"` on the symbol involved, to get the call map inside the repo.
4. Fix it while respecting the layer contract (`uv run lint-imports`).
5. Run the full suite (`uv run pytest`) and `uv run ruff check`.
6. Verify that the response shape did not change: `file_path` without a prefix and the contract golden master green. If the shape has to change, it is a contract change: declare it in the contract history and in an ADR, never silently.
7. Open the pull request with the VCS tool of the repository's remote, attaching the before and after output.
8. Update the vault's tool inventory and the repository's `CLAUDE.md` if the tool goes from "broken" to "available".
9. `mem_save` (`type: bugfix`, `topic_key: knowledge-mcp/tools/$1`).

## Before the pull request

- [ ] Failure reproduced with the exact documented parameters; literal output saved.
- [ ] Regression test seen failing before the fix and passing after it.
- [ ] `lint-imports` with no new violations.
- [ ] Full suite and contract golden master green.
- [ ] Before and after output attached to the pull request.
