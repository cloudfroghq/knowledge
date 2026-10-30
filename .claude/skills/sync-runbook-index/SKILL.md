---
name: sync-runbook-index
description: Reconstruye el índice de runbooks de memory leyendo el vault por las tools del knowledge, para que mem_runbook_find responda sobre lo que el vault dice hoy.
when_to_use: cuando mem_project_card reporta runbooks_stale > 0, cuando synced_at del índice tiene más de 7 días, cuando acabas de fusionar un PR que agrega o renombra un runbook, o antes de un triage si mem_runbook_find no devuelve candidatos para un síntoma que sabes documentado.
allowed-tools: Read, Bash(memory project *), mcp__cloudfrog-knowledge__search_by_metadata, mcp__cloudfrog-knowledge__get_document, mcp__cloudfrog-knowledge__get_stale_docs, mcp__memory__mem_project_card, mcp__memory__mem_runbook_index_sync, mcp__memory__mem_runbook_find, mcp__plugin_memory_memory__mem_save
model: sonnet
effort: medium
disable-model-invocation: false
user-invocable: true
argument-hint: [servicio]
---

# sync-runbook-index

Reconstruye `runbook_index` de memory desde el vault. El índice es una caché derivada: el documento del vault siempre gana, y esta skill solo lo copia hacia memory. Nunca escribe en el vault.

Sin argumento sincroniza todos los servicios; con `$1` acota al servicio indicado.

## Cuándo NO correr esto

- Si `mem_project_card` dice `runbooks_stale: 0` y el índice se sincronizó hace menos de 7 días, no hay nada que hacer: un resync no cambia una fila y gasta 10 llamadas al knowledge.
- Si lo que quieres es corregir un runbook, esta skill no sirve: el vault se edita por PR humano (D-11). Usa `open-pr`.

## Pasos

1. **Medir antes de tocar.** `mem_project_card(project, include_counts=true)` y quédate con `counts.runbooks` y `counts.runbooks_stale`. Sin esa foto no puedes decir después si el sync sirvió de algo.
2. **Enumerar.** `search_by_metadata(type="runbook", limit=200)`. Responde `{total, offset, items}`: los runbooks están en `items`, y mientras `offset + len(items) < total` pide la página siguiente con `offset=offset + len(items)`. Sin recorrer todas las páginas no enumeraste el vault completo.
3. **Leer el frontmatter de cada resultado.** `get_document(file_path, max_chars=1500, full_metadata=true)`. Sin `full_metadata` la `metadata` trae solo la lista blanca de 2.0.0: `type`, `status`, `service`, `owner`, `tags` y `last_updated`. De ahí salen `id`, `title`, `service`, `category`, `pattern`, `severity`, `status`, `symptoms`, `tags`, `owner`, `automation_level`, `last_updated` y `last_verified`. `max_chars=1500` alcanza para el encabezado y evita traerte el runbook entero.
4. **Traer la frescura.** `get_stale_docs(limit=30, type="runbook")` devuelve `needs_review` y `age_days` por `file_path`; existe solo en un vault que declara `features: [governance]` (Cloud frog la declara; sin ella responde `E_FEATURE_DISABLED`). Cruza por `file_path`, no por título: dos runbooks pueden compartir título y ninguno comparte ruta.
5. **Sincronizar.** `mem_runbook_index_sync(source="knowledge-mcp", entries=[...])`, con `prune_missing=true` **solo** si el paso 2 enumeró el vault completo, página por página hasta `total`; con un `$1` que acota el servicio, `prune_missing` borraría del índice runbooks que sí existen.
6. **Leer `skipped[]` — es el entregable, no un detalle.** Cada motivo dice algo distinto y solo dos son normales.
7. **Verificar.** `mem_runbook_find` con un síntoma que sepas documentado (por ejemplo `"preview 503"` para `RB-003`) y confirma `id`, `stale` y `age_days`. Un sync que reporta `upserted` pero cuyo `find` no devuelve nada dejó el FTS sin poblar y hay que investigarlo, no repetirlo.
8. **Guardar el hallazgo si lo hubo.** `mem_save` (`type: discovery`, `topic_key: knowledge-mcp/runbooks/index`) cuando un descarte revela un defecto del vault, con el `file_path` y el motivo exactos.

## Qué significa cada motivo de `skipped[]`

| Motivo | Qué pasó | Qué hacer |
|---|---|---|
| `template` | El documento es una plantilla (tag `template` o `Runbooks/Templates/`) | Nada. Es el filtro funcionando. |
| `not_runbook` | `type` distinto de `runbook`: playbooks y MOCs no se indexan en v1 | Nada. Se alcanzan desde `Related Notes` del runbook. |
| `invalid_id` | El `id` no cumple `RB-NNN`, o el documento no tiene `id` | Defecto del vault. PR con `open-pr` asignando el siguiente id libre (`create-runbook/scripts/next-runbook-id.sh`). |
| `invalid_status` | `status` fuera de `draft\|verified\|outdated` | Defecto del vault. PR corrigiendo el frontmatter. |
| `unknown_service` | El `service` no está en la lista canónica de 15 slugs | Casi siempre una plantilla con el enum literal, o un servicio nuevo. Si el servicio existe de verdad, el mapa vive en `internal/runbooks/service_map.go` del fork de memory y se amplía ahí, no aquí. |
| `missing_service` | El documento no declara `service` | Defecto del vault. PR agregándolo. |

Ninguno de estos motivos se arregla escribiendo en el vault desde la sesión: el vault se edita por PR humano con el dueño del servicio como revisor.

## Ruta alterna sin MCP

Con un clon del repositorio del vault a mano, el mismo índice se reconstruye sin el servidor:

```bash
memory project <slug> runbooks sync \
  --vault-dir "$HOME/Projects/Vaults/<slug>" --json
```

El vault no vive en este repositorio: `--vault-dir` es la `path` del vault en
`~/.config/knowledge/vaults.yaml` (o el `VAULT_PATH` del proceso), que en la
máquina del dueño es `~/Projects/Vaults/<slug>`. No la derives de
`git rev-parse --show-toplevel` de este checkout: ahí ya no hay ningún vault.

Cambia dos cosas respecto de la ruta MCP: `source` es `vault-fs` y `stale` se deriva de `age_days > 90` en lugar de venir de `needs_review`. Los descartes son los mismos, más `not_runbook`, que por MCP no aparece porque `search_by_metadata(type="runbook")` ya lo filtró.

Ver `checklist.md` antes de dar el índice por bueno.
