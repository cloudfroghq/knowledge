# Checklist — repair-mcp-tool

- [ ] Fallo reproducido con los parámetros exactos documentados; salida literal del error guardada.
- [ ] Test `pytest-asyncio` de regresión escrito contra `tests/fixtures/vault/` y visto en rojo antes del fix.
- [ ] `graphify explain` corrido sobre el símbolo implicado.
- [ ] El fix respeta las fronteras de capa (`indexer`/`graph`/`usage`/`tools`); `import-linter` no reporta violación nueva.
- [ ] Suite completa + suite de contrato del RFC en verde.
- [ ] Forma de la respuesta verificada contra el esquema congelado (`file_path` sin prefijo, sin corchetes de wikilink).
- [ ] PR abierto con la salida antes/después adjunta.
- [ ] Inventario de tools del servidor en el vault y `CLAUDE.md` del paquete actualizados si la tool cambia de estado.
- [ ] `mem_save` con `topic_key: knowledge-mcp/tools/<tool>` completado.
