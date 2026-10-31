---
name: repair-mcp-tool
description: Fixes a broken knowledge MCP tool without breaking the ones that work.
when_to_use: when a knowledge tool is documented as broken and has (or needs) a regression test in red with the exact input that fails. Today no tool is documented as broken.
allowed-tools: Read, Write, Edit, Bash(uv run *), Bash(pytest *), Bash(ruff *), Bash(mypy *), Bash(graphify *), Bash(gh pr *), mcp__cloudfrog-knowledge__get_document, mcp__plugin_memory_memory__mem_save
model: sonnet
effort: high
disable-model-invocation: false
user-invocable: true
argument-hint: <tool-name>
---

# repair-mcp-tool

Fix the tool $1 without breaking the remaining tools of the catalog.

## Steps

1. Reproduce the failure with the exact documented parameters and save the literal error output.
2. Write the `pytest` test that reproduces that failure, against `tests/fixtures/vault/`, and watch it fail (red).
3. `graphify explain "<function>"` on the symbol involved, for the call map within the repo.
4. Fix it, respecting the layer contract in `.importlinter` (`uv run lint-imports`).
5. Run the full suite plus the RFC's contract suite, because a coroutine awaited one time too many also breaks something different.
6. Verify that the response shape meets the frozen schema (`file_path` without a prefix) and that the golden master (`tests/test_contract_golden.py`) stays green or is re-recorded on purpose, declaring the change as a contract change in the corresponding ADR.
7. `gh pr create --base main --title "fix(tools): <tool> ..." --body "..."` attaching the before and after output.
8. Update the server's tool inventory in the vault and the package's `CLAUDE.md` if the tool goes from "broken" to "available".
9. `mem_save` (`type: bugfix`, `topic_key: knowledge-mcp/tools/$1`).

See `checklist.md` before opening the PR.
