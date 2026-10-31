# Checklist — repair-mcp-tool

- [ ] Failure reproduced with the exact documented parameters; literal error output saved.
- [ ] `pytest-asyncio` regression test written against `tests/fixtures/vault/` and seen in red before the fix.
- [ ] `graphify explain` run on the symbol involved.
- [ ] The fix respects the layer boundaries (core/ext/http, `.importlinter`); `import-linter` reports no new violation.
- [ ] Full suite + the RFC's contract suite green.
- [ ] Response shape verified against the frozen schema (`file_path` without a prefix, without wikilink brackets).
- [ ] PR opened with the before/after output attached.
- [ ] Server's tool inventory in the vault and the package's `CLAUDE.md` updated if the tool changes status.
- [ ] `mem_save` with `topic_key: knowledge-mcp/tools/<tool>` completed.
