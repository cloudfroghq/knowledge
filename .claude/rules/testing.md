---
paths: ["src/knowledge/**", "tests/**"]
---

# Testing

- Ningún test usa el vault de producción: todo corre contra `tests/fixtures/vault/`.
- Toda tool tiene al menos un test asíncrono que la invoca por su interfaz real, y un test de esquema que congela la forma de la respuesta.
- Una reparación entra con su test de regresión en rojo primero, usando los parámetros documentados como fallidos hoy.
- Los umbrales de cobertura viven en `pyproject.toml`, nunca solo en un documento.
- El pipeline corre en Linux, no en macOS: la insensibilidad a mayúsculas de macOS ya ocultó un defecto real.
- La suite usa el almacén real: un `SQLiteStore` sobre un directorio temporal, con FTS5 y `sqlite-vec`. Los embeddings los pone el proveedor determinista `bag-of-words` (`KNOWLEDGE_EMBEDDINGS_PROVIDER=bag-of-words`); ninguna prueba llama a OpenAI ni parchea el almacén.
- `KNOWLEDGE_REQUIRE_SQLITE_VEC=1` convierte el respaldo `python` (la extensión no carga en ese Python) en el error `E_SQLITE_VEC_UNAVAILABLE`: el pipeline y la grabación de goldens lo fijan, y los goldens se graban solo con el Python 3.12 de `uv`.
- La suite de contrato del RFC es bloqueante y se actualiza junto con el RFC, nunca después.
