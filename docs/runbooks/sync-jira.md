# Sincronizar Jira con las tareas de memory

Runbook para quien quiere ver los issues de Jira de un proyecto en `mem_task_list` de su memory local. La extensión `jira` lee los issues de los proyectos que el vault declara y hace upsert de una tarea de memory por issue, con `jira_key` como clave. No escribe en Jira ni en el vault: lee Jira, escribe en memory y guarda su cursor junto al índice del vault.

Solo corre por stdio. Un despliegue HTTP no la carga (`W_EXTENSION_UNAVAILABLE_HTTP`): el memory donde escribe escucha en el loopback de la persona.

## Alcance

Solo sincroniza Jira un vault que declara `integrations.jira`; `jira sync --auto` y `sync_jira` ignoran los demás. Para activarlo en un vault hay que añadir el bloque del paso 1 en un PR de ese vault y exportar `<SLUG>_JIRA_EMAIL` y `<SLUG>_JIRA_TOKEN` (con el slug del vault en mayúsculas como prefijo) en el entorno de tu sesión; ningún valor se guarda en el repo.

## 1. Declararla en el vault

Por PR del vault, con el dueño como revisor (D-11):

```yaml
features: [jira, memory-bridge]
memory:
  project: acme
integrations:
  jira:
    site: https://acme.atlassian.net
    projects: [ACME]
    jql_extra: ""                      # opcional; se añade como AND (...)
    kind_map: { default: feature }     # opcional; ver la tabla de abajo
    status_map: {}                     # opcional; casi nunca hace falta
    sync: { throttle_minutes: 60 }     # minutos entre corridas del hook de sesión
    secrets: { email_env: ACME_JIRA_EMAIL, token_env: ACME_JIRA_TOKEN }
```

Reglas que se comprueban al cargar el `vault.yaml` (`E_MANIFEST_INVALID` con la ruta de la clave):

| Clave | Regla |
|---|---|
| `site` | `https` en `*.atlassian.net` o en un host que el operador liste en `KNOWLEDGE_INTEGRATION_SITES`, sin usuario ni contraseña en la URL; `http` solo en `127.0.0.1`, `localhost` o `::1` |
| `projects` | Lista de claves de proyecto (`ACME`, `OPS`) |
| `secrets.email_env`, `secrets.token_env` | Nombres de variable en mayúsculas, con `JIRA` como segmento, terminados en `_EMAIL` y `_TOKEN` y con el `slug` del vault como prefijo (`ACME_JIRA_EMAIL`); sin prefijo (`JIRA_API_TOKEN`) solo el vault por defecto, y nunca la variable de otro vault. Nunca un valor: uno con forma de credencial es `E_SECRET_IN_MANIFEST` |
| `kind_map` | Tipo de issue → `feature`, `bugfix`, `refactor`, `incident`, `migration` o `spike`; la clave `default` cubre los tipos que no nombra |
| `status_map` | Estado de Jira → `open`, `analysis`, `in_progress`, `review`, `verified`, `done`, `blocked` o `cancelled` |
| `sync.throttle_minutes` | Entero, 0 o más |
| `target` | Solo `memory` |

`site`, `projects` y los dos nombres de `secrets` se exigen cuando la sincronización corre, no al cargar: la card de `memory-bridge` lee `projects` sola. Un servidor HTTP no carga `jira`, así que un valor inválido en `projects`, `jql_extra`, `kind_map`, `status_map`, `sync` o `target` solo le da `W_EXTENSION_UNAVAILABLE_HTTP`; `site` y `secrets` lo detienen por cualquier transporte.

## 2. Qué viaja a memory

Una tarea por issue, en serie, a `POST /projects/<memory.project>/tasks`:

| Campo | De dónde sale |
|---|---|
| `jira_key` | La clave del issue (`ACME-1234`); una clave con otra forma se salta como `invalid_key` |
| `title` | El resumen, hasta 300 caracteres; vacío, la clave |
| `kind` | `kind_map[tipo]`, si no el default por tipo (`Bug` → `bugfix`, `Incident` → `incident`, `Spike` → `spike`), si no `kind_map.default`, si no `feature` |
| `jira_status` | El nombre del estado, tal como lo da Jira |
| `jira_status_category` | `new`, `indeterminate` o `done`, tal como lo da Jira. Si Jira da otra cosa, no viaja y el estado entra en `status_unmapped` |
| `assignee` | El nombre visible del responsable, si hay |
| `state` | Solo si `status_map` nombra el estado |

memory deriva `state` de `jira_status` con su propio mapa y, si no conoce el literal, de `jira_status_category`. Por eso `status_map` casi nunca hace falta: sirve para un estado propio de la organización que memory no conoce.

## 3. Credenciales y memory

```bash
# fish: exporta en tu configuración, nunca en un archivo del repo
set -gx ACME_JIRA_EMAIL tu.correo@empresa.com
set -gx ACME_JIRA_TOKEN <api token de id.atlassian.com>
```

La escritura usa el bearer de memory (`MEMORY_HTTP_TOKEN`). El `memory serve` local corre hoy sin token, así que para él vale `KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0` (ver `docs/install.md`, sección del memory-bridge). Un `--dry-run` no necesita ni uno ni otro.

Crea la card del proyecto antes de la primera escritura, para que lleve el `jira_project` correcto:

```bash
KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0 uv run cloudfrog-knowledge memory-bridge sync --vault acme --card
```

## 4. Correr

```bash
uv run cloudfrog-knowledge doctor | jq '.vaults[] | select(.slug=="acme") | .jira'
uv run cloudfrog-knowledge jira sync --vault acme --dry-run --since -30d --json | jq '{fetched, skipped, status_unmapped}'
KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0 uv run cloudfrog-knowledge jira sync --vault acme --json | jq '{fetched, upserted, created, skipped}'
curl -s "http://127.0.0.1:6366/projects/acme/tasks?limit=5" | jq '.total, [.items[].jira_key]'
```

Una segunda corrida da `created: 0`: el upsert es idempotente.

Desde un cliente MCP por stdio, `sync_jira(vault="acme")` hace lo mismo que `--dry-run` y `sync_jira(vault="acme", dry_run=false)` escribe. La tool aparece solo cuando algún vault servido declara `jira`, al final del catálogo.

Con el plugin `knowledge`, el hook de sesión lanza `cloudfrog-knowledge jira sync --auto` en segundo plano cuando `doctor` sale bien: sincroniza cada vault que declara `jira`, tiene sus variables exportadas y ya cumplió su `throttle_minutes`. No bloquea la sesión ni imprime nada. `KNOWLEDGE_JIRA_AUTOSYNC=0` lo apaga.

## 5. Desde cuándo

- `--since` (o `since=` en la tool): fecha ISO (`2026-09-01`), fecha y hora (`2026-09-01T12:00:00Z`) o `-Nd` (`-7d`). Gana siempre.
- Sin él, después de una corrida completa: el cursor menos un día de solape; repetir un issue no cuesta nada.
- Sin él, después de una corrida truncada: el cursor exacto, sin solape, para que una cola de más de 2.000 issues siempre avance.
- Sin cursor: 30 días.

JQL lee las fechas en la zona horaria de la cuenta de Jira, así que la fecha que va en `jql` está escrita en esa zona (la que responde `GET /rest/api/3/myself`, por ejemplo `2026-10-05 14:45` para las `17:45` UTC en Buenos Aires), mientras `since` y `next_since` quedan en UTC. Si Jira no dice la zona, la fecha se escribe en UTC-12: la consulta empieza antes de lo necesario y nunca después.

El cursor (`next_since`) es el `updated` del último issue leído, en UTC, y vive en `jira_sync.json` junto al `index.sqlite` del vault, con `truncated`, `retry`, `synced_at`, `last_attempt_at` y `last_result`. Solo cambia tras una corrida que escribió y en la que Jira y memory respondieron. Una corrida de más de 2.000 issues (o de más de `--max-issues N` en la CLI) termina con `truncated: true` y la siguiente sigue desde su `next_since`; la búsqueda ordena por `updated` y luego por clave, y JQL compara minutos, así que la corrida siguiente vuelve a leer los issues del minuto donde se cortó (el upsert es idempotente). Si todos los issues que cabían en la corrida comparten ese minuto, el cursor no puede avanzar y la corrida responde `E_JIRA_SYNC_STALLED` sin escribir nada.

El cursor nunca deja atrás un issue que no llegó a memory:

- Una tarea que memory rechaza por su contenido (`invalid_enum`, `task_key_conflict`) queda en `retry`, y la corrida siguiente también la lee por clave (`GET /rest/api/3/issue/<clave>`); sale de la lista cuando memory la acepta o cuando Jira ya no la tiene (404). La lista guarda hasta 50 claves; si memory rechaza más, el cursor se queda en el `updated` de la más vieja que no cupo.
- Un `--since` posterior al cursor guardado lee desde ahí pero deja el cursor donde estaba: los issues entre los dos siguen pendientes para la próxima corrida sin `--since`.

## 6. Reporte

```json
{
  "vault": "acme", "memory_project": "acme", "dry_run": false,
  "jql": "project in (ACME) AND updated >= \"2026-09-12 15:00\" ORDER BY updated ASC, key ASC",
  "since": "2026-09-12T15:00:00Z", "next_since": "2026-10-12T14:58:00Z", "truncated": false,
  "fetched": 126, "retried": 0, "upserted": 125, "created": 125,
  "skipped": [{"key": "ACME-77", "reason": "task_key_conflict"}],
  "status_unmapped": []
}
```

`retried` cuenta los issues que la corrida volvió a leer por clave porque memory los rechazó en la última corrida. `skipped.reason` es `invalid_key`, el código propio de memory (`task_key_conflict`: la clave ya es de una tarea de otro proyecto; `invalid_enum`; `missing_field`) o `memory_unavailable`. Si memory deja de responder a mitad de la corrida, las tareas que faltaban quedan en `skipped`, el reporte trae `error` con su código y el cursor no avanza.

## 7. Códigos

| Código | Qué hacer |
|---|---|
| `E_FEATURE_DISABLED` | El vault no declara `jira`: añádelo a `features` por PR del vault |
| `E_MEMORY_PROJECT_MISSING` | El vault no declara `memory.project` |
| `E_MANIFEST_INVALID` | Falta `site`, `projects` o un nombre de `secrets`, o un valor no cumple la tabla de la sección 1 |
| `E_SECRET_MISSING` | Las variables que nombra `secrets` están vacías; el mensaje dice cuáles |
| `E_MEMORY_TOKEN_MISSING` | Una escritura sin `MEMORY_HTTP_TOKEN`: expórtalo, o `KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0` para el memory local sin token |
| `E_MEMORY_URL_NOT_LOOPBACK` | `KNOWLEDGE_MEMORY_URL` no es `http` en loopback |
| `E_JIRA_AUTH_FAILED` | Jira rechazó el correo y el token (401 o 403) |
| `E_JIRA_UNAVAILABLE` | Jira no respondió con issues: un 400 (revisa `projects` y `jql_extra`), un 5xx, más de 10 s, o un 429 repetido |
| `E_JIRA_SYNC_STALLED` | Todos los issues que caben en la corrida se actualizaron en el minuto donde empieza, y el cursor no puede pasarlo: sube `--max-issues` (hasta 2.000), acota `jql_extra` o pasa un `--since` posterior a ese minuto |
| `E_ARGUMENT_OUT_OF_RANGE` | `since` no se entiende o está en el futuro |
| `E_SCOPE_REQUIRED` | La llamada llegó con un bearer sin `admin` |

`jira sync` sale con 0 si manejó todos los issues, 1 con un error de Jira (`E_JIRA_*`) o memory caído a mitad, y 2 si no pudo intentar nada. `--auto` sale siempre con 0, falle lo que falle. Ningún mensaje, reporte ni línea de log lleva el token, el correo ni el cuerpo que respondió Jira o memory.
