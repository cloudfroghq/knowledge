---
paths:
  - "*.md"
  - "**/*.py"
  - "pyproject.toml"
---

# Branch patterns and merge destinations

Source of authority: Confluence page "Branch" (id 6421610526) adopting Conventional Branch.

## Pattern reference

Branch names follow the regex pattern:

```
^(feature|feat|fix|bugfix|hotfix|release|chore|refactor|docs|test|ci|perf|style|build|revert)/([A-Z]+-\d+(-[a-z0-9-]+)?|[a-z0-9][a-z0-9-]*)$
```

## Branch types by work type

| Work type | Pattern | Example | Merge destination |
|---|---|---|---|
| New tool or response-shape change | `feature/PROJ-NNNNN` | `feature/PROJ-11002` | `main` |
| Repair of a broken tool (`search_by_metadata`, `search_deep`, `compare_flows`) | `fix/PROJ-NNNNN` | `fix/PROJ-11003` | `main` |
| Indexing bugfix | `fix/PROJ-NNNNN` | `fix/PROJ-11004` | `main` |
| Layer refactor (core/ext/http layers, `.importlinter`) | `refactor/PROJ-NNNNN` or `refactor/<kebab-desc>` | `refactor/tools-layer-boundary` | `main` |
| Chore | `chore/<kebab-desc>` | `chore/ruff-baseline-refresh` | `main` |
| Documentation (server code, not the vault) | `docs/<topic>` | `docs/tool-inventory-refresh` | `main` |
| Release | Annotated tag, no branch | `vX.Y.Z` | N/A (the tag is the release) |

## Vault discipline

1. No vault lives in this repo: each one has its own repository. A code PR in this repo never carries vault content, and `/vault/` is in `.gitignore`. In a vault repo, only `90 - Memory/` is written by the automatic bridge (`scripts/weekly-memory-bridge.sh`, which commits and pushes in the vault's repository).
2. A vault content change is declared by human PR with the owner as reviewer (D-11); the agent proposes, and never writes outside `90 - Memory/`.
3. In a clone of a vault repo, never `git add -A` or `git add .` when the working tree mixes `90 - Memory/` with other local changes — always explicit paths.

## Verification rules

1. Branch name passes the regex: `git branch --show-current | rg -q '^(feature|feat|fix|bugfix|hotfix|release|chore|refactor|docs|test|ci|perf|style|build|revert)/([A-Z]+-[0-9]+(-[a-z0-9-]+)?|[a-z0-9][a-z0-9-]*)$'` returns 0.
2. Branch created from updated `origin/main`: `git fetch origin && git checkout -b <branch> origin/main`, never from another work branch.
3. The ring 1 pipeline runs on Linux: a new path is verified with real case sensitivity before the PR (macOS can hide a conflict that does break there).
4. Ring 0 (`.claude/hooks/run-gates.sh`) scopes everything to what the push adds: it anchors on the tracking branch (`@{upstream}..HEAD`) and, if the branch does not have one yet, on the merge base with the nearest anchor (`origin/HEAD`, then `origin/main`, then `origin/master`). Over that range it runs `gitleaks`, plus `uv run ruff check` and `uv run pytest -q` when `.py` files are touched. Export `RUN_GATES_BASE_REF` (one or more refs separated by spaces) when no generic anchor resolves; `RUN_GATES_SKIP=1` bypasses it deliberately.
