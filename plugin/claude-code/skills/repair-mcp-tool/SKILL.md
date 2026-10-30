---
name: repair-mcp-tool
description: Arregla una tool MCP rota del servidor knowledge sin romper las que funcionan.
when_to_use: cuando una tool del servidor knowledge está documentada como rota y tiene (o necesita) un test de regresión en rojo con la entrada exacta que falla.
allowed-tools: Read, Write, Edit, Bash(uv run *), Bash(graphify *), mcp__plugin_knowledge_knowledge__get_document, mcp__plugin_memory_memory__mem_save
model: sonnet
effort: high
disable-model-invocation: false
user-invocable: true
argument-hint: <nombre-de-la-tool>
---

# repair-mcp-tool

Arregla la tool $1 sin romper el resto del contrato. Se trabaja en un checkout del repositorio del servidor.

## Pasos

1. Reproducir el fallo con los parámetros exactos documentados y guardar la salida literal del error.
2. Escribir el test de `pytest` que reproduce ese fallo, contra el vault de prueba del repositorio (nunca contra un vault real), y verlo en rojo.
3. `graphify explain "<función>"` sobre el símbolo implicado, para el mapa de llamadas dentro del repo.
4. Corregir respetando el contrato de capas (`uv run lint-imports`).
5. Correr la suite completa (`uv run pytest`) y `uv run ruff check`.
6. Verificar que la forma de la respuesta no cambió: `file_path` sin prefijo y el golden master del contrato en verde. Si la forma tiene que cambiar, es un cambio de contrato: se declara en el historial del contrato y en un ADR, nunca en silencio.
7. Abrir el pull request con la herramienta de VCS del remoto del repositorio, adjuntando la salida antes y después.
8. Actualizar el inventario de tools del vault y el `CLAUDE.md` del repositorio si la tool pasa de "rota" a "disponible".
9. `mem_save` (`type: bugfix`, `topic_key: knowledge-mcp/tools/$1`).

## Antes del pull request

- [ ] Fallo reproducido con los parámetros exactos documentados; salida literal guardada.
- [ ] Test de regresión visto en rojo antes del arreglo y en verde después.
- [ ] `lint-imports` sin violaciones nuevas.
- [ ] Suite completa y golden master del contrato en verde.
- [ ] Salida antes y después adjunta al pull request.
