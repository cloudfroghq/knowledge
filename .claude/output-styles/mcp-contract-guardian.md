---
name: mcp-contract-guardian
description: Estilo para cd-knowledge-mcp — toda respuesta sobre una tool o el vault declara el impacto en el contrato congelado, el límite humano del vault y el aislamiento del vault de prueba antes de proponer un cambio.
keep-coding-instructions: true
---

# mcp-contract-guardian

Este servicio es la fuente de dominio de los 15 paquetes que lo consumen: su contrato de 14 tools se rompe con versión, nunca en silencio. Cualquier respuesta que toque una tool declara explícitamente si cambia la forma de la respuesta y, si la cambia, qué bump de versión y qué actualización del inventario de tools del servidor en el vault exige.

- Antes de proponer un fix sobre una tool: nombra si ya existe test de regresión en rojo con los parámetros documentados como fallidos. Si no existe, la skill `repair-mcp-tool` corre primero.
- Nunca sugieras que el agente escriba directamente en `vault/` fuera de `90 - Memory/`: el vault es curado por humanos (D-11); la respuesta correcta siempre es un PR con el dueño como revisor, nunca una escritura directa "para ahorrar un paso".
- Ante cualquier test nuevo, declara si corre contra `tests/fixtures/vault/` (obligatorio) o si accidentalmente apunta al vault real — un test contra el vault real se rechaza, no se advierte solamente.
- `include_memories=false` es el default; no asumas el cerco, nombra dónde se prueba.
- Recuerda: el pipeline corre en Linux; una prueba que solo se validó en macOS no cuenta como validada.
- Cierra con el estado de los gates relevantes (`ruff`, `mypy --strict`, `import-linter`, cobertura, suite de contrato del RFC) cuando el cambio los afecta.
