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
| Tool nueva o cambio de forma de respuesta | `feature/PROJ-NNNNN` | `feature/PROJ-11002` | `main` |
| Reparación de tool rota (`search_by_metadata`, `search_deep`, `compare_flows`) | `fix/PROJ-NNNNN` | `fix/PROJ-11003` | `main` |
| Bugfix de indexación | `fix/PROJ-NNNNN` | `fix/PROJ-11004` | `main` |
| Refactor de capas (`indexer`/`graph`/`usage`/`tools`) | `refactor/PROJ-NNNNN` o `refactor/<kebab-desc>` | `refactor/tools-layer-boundary` | `main` |
| Chore | `chore/<kebab-desc>` | `chore/ruff-baseline-refresh` | `main` |
| Documentation (código del servidor, no el vault) | `docs/<topic>` | `docs/tool-inventory-refresh` | `main` |
| Release | Tag anotado, sin rama | `v0.9.0` | N/A (el tag es el release) |

## Disciplina del vault

1. Ningún vault vive en este repo: cada uno tiene su repositorio. Un PR de código de este repo nunca trae contenido de vault, y `/vault/` está en `.gitignore`. En un repo vault, solo `90 - Memory/` lo escribe el bridge automático (`scripts/weekly-memory-bridge.sh`, que hace commit y push en el repositorio del vault).
2. Un cambio de contenido del vault se declara por PR humano con el dueño como revisor (D-11); el agente propone, nunca escribe fuera de `90 - Memory/`.
3. En un clon de un repo vault, nunca `git add -A` ni `git add .` cuando el árbol de trabajo mezcla `90 - Memory/` con otros cambios locales — siempre rutas explícitas.

## Verification rules

1. Branch name passes the regex: `git branch --show-current | rg -q '^(feature|feat|fix|bugfix|hotfix|release|chore|refactor|docs|test|ci|perf|style|build|revert)/([A-Z]+-[0-9]+(-[a-z0-9-]+)?|[a-z0-9][a-z0-9-]*)$'` returns 0.
2. Branch created from updated `origin/main`: `git fetch origin && git checkout -b <branch> origin/main`, never from another work branch.
3. El pipeline de anillo 1 corre en Linux: una ruta nueva se verifica con case-sensitivity real antes del PR (macOS puede ocultar un conflicto que sí rompe ahí).
4. Ring 0 (`.claude/hooks/run-gates.sh`) acota todo a lo que el push añade: ancla en la rama de seguimiento (`@{upstream}..HEAD`) y, si la rama todavía no tiene ninguna, en la base de fusión con el ancla más cercana (`origin/HEAD`, luego `origin/main`, luego `origin/master`). Sobre ese rango corre `gitleaks`, y `uv run ruff check` más `uv run pytest -q` cuando hay `.py` tocados. Exporta `RUN_GATES_BASE_REF` —uno o varios refs separados por espacios— cuando ninguna ancla genérica resuelve; `RUN_GATES_SKIP=1` bypassea deliberadamente.
