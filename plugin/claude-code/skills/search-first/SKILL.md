---
name: search-first
description: Protocolo de lectura del servidor knowledge — buscar y leer solo la sección a la que apunta la fila.
when_to_use: antes de responder sobre arquitectura, flujos, runbooks, contratos de API o convenciones que viven en un vault, y siempre que vayas a abrir un documento del vault.
allowed-tools: mcp__plugin_knowledge_knowledge__search, mcp__plugin_knowledge_knowledge__get_document, mcp__plugin_knowledge_knowledge__search_by_metadata, mcp__plugin_knowledge_knowledge__context_pack, mcp__plugin_knowledge_knowledge__list_vaults
user-invocable: true
argument-hint: <pregunta>
---

# search-first

Encuentra primero, después lee una sección. Nunca pagines un archivo completo.

## Protocolo

1. Busca. Cada fila ya trae `file_path`, `score`, `chunk_id` y `section` (el bloque al que apunta), sin extractos:

   ```
   search(query="$ARGUMENTS")
   ```

2. Elige la fila con mejor `score` cuya `section` responda la pregunta y lee solo ese bloque:

   ```
   get_document(chunk_id=<chunk_id>)
   ```

   El `chunk_id` basta: no hace falta `file_path`, y la lectura se corta en 3.000 caracteres. Si la fila trae `chunk_id: null` (ningún término de la consulta aparece en el archivo), lee `get_document(file_path=<file_path>)`. Si el bloque no alcanza, lee el `chunk_id` vecino o la siguiente fila de `search`; no subas `max_chars` para traer el documento entero.

3. Si la tarea pide contexto de varias fuentes en una sola llamada, usa `context_pack(query, budget_tokens=3000)` en lugar de encadenar lecturas.

4. Para filtrar por metadatos (tipo, dueño, etiqueta, estado) usa `search_by_metadata` antes que una búsqueda semántica. Responde `{total, offset, items}` con 20 filas por página: lee `items` y pide la siguiente con `offset` mientras `offset + len(items) < total`.

5. Con varios vaults registrados, `list_vaults` dice cuáles hay y cuál es el predeterminado; pasa `vault=<slug>` a cualquier tool para consultar otro.

6. Si el vault declara un juez (bloque `rerank` de su `vault.yaml`), `search` puede reordenar las filas con él: con `rerank.default: on` lo hace sin que lo pidas; con `default: off`, pásale `rerank="on"`. Las filas reordenadas traen `rerank_score` (probabilidad de que el bloque responda) y su `chunk_id` y `section` nombran el bloque que el juez leyó: lee ese. `rerank="off"` lo evita. `rerank="on"` en un vault sin juez responde `E_RERANK_NOT_CONFIGURED`: omite el argumento.

## Reglas

- `file_path` es relativo al vault: pásalo tal cual, sin prefijos ni corchetes de wikilink.
- Todo error trae un código `E_` estable seguido de un mensaje accionable (a veces después del prefijo `Error executing tool …:`): corrige el argumento que nombra, no reintentes igual.
- Cita el `file_path` y la `section` de donde sale cada afirmación.
- Si nada del vault responde, dilo; no rellenes con suposiciones sobre el código de otro servicio.
- Si la pregunta es cómo funciona algo en el código de un checkout y no nombra un identificador, usa la skill `code-question` (`jg` sobre una carpeta estrecha), nunca sobre el vault.
- Para preguntas sobre código no hay tool de knowledge (`search_code` se retiró en el contrato 2.0.0): usa la skill `code-question` (`jg`, graphify, `rg`).
- `get_document` devuelve en `metadata` solo `type`, `status`, `service`, `owner`, `tags` y `last_updated`; pide `full_metadata=true` si necesitas otra clave del frontmatter.
