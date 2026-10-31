## Summary

<!-- What changes and why, in a few sentences. -->

## Type

<!-- Conventional Commit type of the PR title: feat, fix, refactor, docs, test, ci, chore, perf, build, revert. -->

- [ ] feat
- [ ] fix
- [ ] refactor
- [ ] docs
- [ ] test / ci / chore / build / perf

## Tool contract impact

- [ ] No tool response shape, parameter or catalog change
- [ ] Response shape changes: contract bumped in `src/knowledge/contract.py` (`CONTRACT_VERSION`, `TOOL_SINCE`)
- [ ] New goldens recorded under `tests/golden/v<version>/` (frozen ones are never edited)
- [ ] `docs/tools/inventory.md` regenerated (`uv run python scripts/tool_inventory.py`)
- [ ] `docs/contract/CHANGELOG.md` updated

## Methodology

<!-- Keep the row that applies (D-05). -->

| Type of work | Route |
|---|---|
| New tool or response-shape change | Full SDD + RDD High; schema test + contract suite |
| Broken-tool repair | SDD-lite; regression test red first with the exact failing input |
| Indexing bugfix (<= 3 files) | Direct; reproduce with `tests/fixtures/vault/` |
| Layer refactor | SDD + characterization; golden master, empty diff; `lint-imports` |
| Other | Describe |

## Tests

<!-- Tests run against `tests/fixtures/**` only, never a real vault. Describe what you added or why none is needed. -->

## Gates

- [ ] `uv run pytest`
- [ ] `uv run ruff check`
- [ ] `uv run lint-imports`
- [ ] `(cd npm && npm test)`

## Notes for reviewers

<!-- Risk, rollback, follow-ups, anything non-obvious. No tokens, keys or private vault content. -->
