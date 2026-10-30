---
name: repair-mcp-tool
description: Arregla una tool MCP rota de knowledge sin romper las que funcionan.
when_to_use: cuando una tool de knowledge está documentada como rota y tiene (o necesita) un test de regresión en rojo con la entrada exacta que falla. Hoy ninguna tool está documentada como rota.
allowed-tools: Read, Write, Edit, Bash(uv run *), Bash(pytest *), Bash(ruff *), Bash(mypy *), Bash(graphify *), Bash(gh pr *), mcp__cloudfrog-knowledge__get_document, mcp__plugin_memory_memory__mem_save
model: sonnet
effort: high
disable-model-invocation: false
user-invocable: true
argument-hint: <nombre-de-la-tool>
---

# repair-mcp-tool

Arregla la tool $1 sin romper las 13 restantes.

## Pasos

1. Reproducir el fallo con los parámetros exactos documentados y guardar la salida literal del error.
2. Escribir el test de `pytest` que reproduce ese fallo, contra `tests/fixtures/vault/`, y verlo en rojo.
3. `graphify explain "<función>"` sobre el símbolo implicado, para el mapa de llamadas dentro del repo.
4. Corregir, respetando el contrato de capas de `.importlinter` (`uv run lint-imports`).
5. Correr la suite completa más la suite de contrato del RFC, porque una corrutina esperada de más también rompe algo distinto.
6. Verificar que la forma de la respuesta cumple el esquema congelado (`file_path` sin prefijo) y que el golden master (`tests/test_contract_golden.py`) sigue en verde o se regraba a propósito declarando el cambio como cambio de contrato en el ADR correspondiente.
7. `gh pr create --base main --title "fix(tools): <tool> ..." --body "..."` adjuntando la salida antes y después.
8. Actualizar el inventario de tools del servidor en el vault y el `CLAUDE.md` del paquete si la tool pasa de "rota" a "disponible".
9. `mem_save` (`type: bugfix`, `topic_key: knowledge-mcp/tools/$1`).

Ver `checklist.md` antes de abrir el PR.
