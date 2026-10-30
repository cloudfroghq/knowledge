---
description: Resuelve un ticket de bug de EXMP en knowledge de punta a punta, con test de regresión y PR listo para revisión.
allowed-tools: mcp__mcp-atlassian__jira_get_issue, mcp__mcp-atlassian__jira_add_comment, mcp__mcp-atlassian__jira_transition_issue, Bash(git *), Bash(gh pr create *), Bash(uv run pytest *), Edit, Write, Read
argument-hint: <EXMP-NNNN>
---

# fix-issue

Resuelve el ticket $1 de punta a punta.

1. `mcp__mcp-atlassian__jira_get_issue` sobre $1 para obtener síntoma, tool o componente afectado y criterios de aceptación.
2. Crear la rama `fix/$1` desde `main` actualizado.
3. Si el ticket es sobre una tool MCP, ejecutar la skill `repair-mcp-tool` en vez de continuar este flujo directo.
4. Escribir el test de regresión primero y confirmarlo en rojo (`uv run pytest -k <caso>`).
5. Implementar el fix mínimo que hace pasar el test.
6. `gh pr create --base main --title "fix: <resumen>" --body "<detalle, incluye $1>"`.
7. `mcp__mcp-atlassian__jira_add_comment` con el resumen técnico del fix y el enlace del PR.
8. `mcp__mcp-atlassian__jira_transition_issue` a "In Code Review".

No se escribe en `vault/` fuera de `90 - Memory/` como parte de este comando: un cambio de contenido del vault va por PR humano (D-11), nunca por este flujo.
