---
name: create-runbook
description: Redacta un runbook nuevo desde los síntomas y las ejecuciones conocidas de un ticket, con validación antes de abrir PR.
when_to_use: cuando triage levanta un ticket que es un caso de procedimiento conocido, cuando un operador necesita documentar una solución después de resolver un incidente, o cuando `mem_runbook_find` devuelve un runbook cuyo procedimiento quedó inactual tras cambios en la infraestructura.
allowed-tools: Read, Edit, Bash(memory project *), Bash(.claude/skills/create-runbook/scripts/next-runbook-id.sh), Bash(.claude/skills/create-runbook/scripts/instantiate-runbook.sh), Bash(.claude/skills/create-runbook/scripts/validate-runbook.sh), mcp__mcp-atlassian__jira_get_issue, mcp__cloudfrog-knowledge__search, mcp__cloudfrog-knowledge__search_by_metadata, mcp__plugin_memory_memory__mem_search, mcp__plugin_memory_memory__mem_save, AskUserQuestion, open-pr
disable-model-invocation: false
user-invocable: true
argument-hint: "<TICKET-KEY[,TICKET-KEY…]> <pattern>"
---

# create-runbook

No confundir con `design-runbook`, la skill que traen algunos paquetes de
contexto de servicio: esa documenta directamente en `cloudfrog-knowledge`, sin
staging ni PR, el diagnóstico que ya resolvió `resolve-ticket`. Esta redacta el
runbook curado del vault y sí exige staging, `validate-runbook.sh` y PR humano
(D-11).

Redacta un runbook nuevo en ≤ 15 minutos con confirmación humana del diagnóstico.

## Cuándo NO correr esto

- Si el síntoma ya está documentado en `mem_runbook_find`, esta skill no sirve para renovarlo: el vault se edita por PR humano (D-11). Usa `open-pr` con el archivo específico del vault si necesitas cambios.
- Si el ticket es un incidente puntual sin patrón repetible, no es un runbook: es un registro histórico de la resolución en memory (`mem_save` con `type: incident`).
- Si el pattern aún no está en el enum de la plantilla (`missing-files`, `auth-access`, `file-save-failure`, `sync-upload`, `registration-subscription`, `other`), elige el más cercano o declara `other` y documenta qué patrón hace falta.

## Pasos

### 1. Entrada y síntomas literales (minuto 0–2)

1. Lee el ticket con `jira_get_issue(KEY, comment_limit=20)` y extrae: síntomas reportados, país afectado, partner (si aplica), `issue_type`.
2. Toma nota del KEY para usar más adelante (`jira_refs`).

### 2. Causa raíz conocida y ejecuciones previas (minuto 2–4)

1. Busca en memory: `mem_search(incident/<KEY>)` para ver si hay un incidente anterior con el mismo síntoma.
2. Busca en el knowledge: `search(query=<síntomas del ticket>)` para encontrar runbooks por síntoma, y `search_by_metadata(type="runbook")` para la lista completa (responde `{total, offset, items}`: lee `items` y pide la página siguiente con `offset` mientras `offset + len(items) < total`).
3. Si encuentras un runbook candidato, contrasta: ¿el procedimiento funciona todavía? ¿Los pasos de resolución siguen siendo válidos? Si no, delega en `open-pr` para actualizar el existente en lugar de crear uno nuevo.

### 3. Reservar id (minuto 4–5)

1. Ejecuta `.claude/skills/create-runbook/scripts/next-runbook-id.sh` (sin argumentos).
2. Guarda el `RB-NNN` que devuelve.

### 4. Redacción en staging (minuto 5–9) — FRONTERA CON EL VAULT

**AQUÍ OCURRE EL CONFLICTO CON `guard-vault-write.sh`:**

El hook `guard-vault-write.sh` (PreToolUse sobre Write|Edit) bloquea cualquier escritura bajo los clones de vault (`~/Projects/Vaults/<slug>/`, o `KNOWLEDGE_VAULTS_ROOT`) excepto `90 - Memory/`. La razón: el vault es contenido curado por personas, no por agentes (ADR D-11), y vive en su propio repositorio, no en este. La sección 6 de la plantilla-runbook.md dice escribir directamente bajo `Runbooks/` del vault, pero ese destino está prohibido por el hook.

**Resolución:** El documento se redacta en una **ruta de staging**, FUERA del vault:

```
$MEMORY_PROJECT_STAGING/create-runbook/RB-NNN <Title>.md
```

(Por defecto: `$HOME/.local/share/knowledge/staging/create-runbook/`)

1. Ejecuta `.claude/skills/create-runbook/scripts/instantiate-runbook.sh --id <RB-NNN> --pattern <pattern> --out-dir "$MEMORY_PROJECT_STAGING/create-runbook" --title "<título>" --jira-refs <KEY> [--fixture <ticket.json>]`. El script lee los defaults del `pattern` desde `create-runbook/patterns/<pattern>.md` (categoría, servicio, `tools[]`, `evidence_required[]` y el `knowledge_ref` a la plantilla por categoría — ADR-032 punto 3), extrae el esqueleto literal de `ai-platform-plan/05-runbooks/plantilla-runbook.md` sección 3 y sustituye cada variable del frontmatter por un valor real (nunca deja un `{{...}}` sin resolver ahí: no sería YAML válido). Los síntomas, el país y las referencias de Jira vienen del ticket leído en el paso 1 (o del fixture, si se está ensayando).
2. Añade a mano los pasos del diagnóstico (`{{quick_step}}`, `{{mutating_step}}`, `{{validation_command}}`, ...) y la causa raíz confirmada: esas variables del CUERPO se dejan sin resolver a propósito — necesitan el diagnóstico humano del paso siguiente, y `validate-runbook.sh` check 9 las espera ahí.
3. El archivo ya nace en staging con `status: draft`. No toca el vault.
4. Nota: El documento irá al vault cuando una persona ejecute los scripts de aplicación (paso siguiente en el flujo operativo, no en esta skill).

### 5. Confirmación humana del diagnóstico y los pasos (minuto 9–11)

1. Presenta el documento draft a la persona con `AskUserQuestion`:
   - "¿El diagnóstico y los pasos coinciden con lo que resolvió el incidente?"
   - "¿Falta algún paso? ¿Alguno es incorrecto o demasiado específico?"
   - "¿El procedimiento sigue siendo válido en el código/infra actual?"
2. Incorpora ajustes si los hay. Vuelve a guardar en staging.

### 6. Validación (minuto 11–12)

1. Ejecuta `.claude/skills/create-runbook/scripts/validate-runbook.sh <archivo-en-staging>`.
2. Si exit 0, continúa. Si no, reporta los errores a la persona y repite los ajustes.
3. Los checks del validador incluyen: frontmatter YAML completo con enums, id único en el vault, síntomas y referencias, herramientas conocidas, secciones en orden, bloques mermaid, pasos [GATE] con rollback, sin PII, idioma, etc.

### 7. Preparación del índice y PR (minuto 12–14)

1. Agrega una fila a la tabla de `Runbooks.md` del vault (el index). No edites el vault todavía; prepara la fila en staging.
2. Si el `pattern` es nuevo (no aparece en `Issue Patterns from Jira.md` del vault), actualiza ese documento también en staging.
3. Redacta el cuerpo del PR con el título `docs(runbooks): add RB-NNN <slug>` y el changelog.
4. **Delega el resto en `open-pr`**: esa skill maneja el PR contra `main` de `knowledge` en la rama `docs/rb-nnn-<slug>`. Ella abre, asigna revisor y notifica.
5. Nota: El merge del PR es paso manual, humano.

### 8. Aplicación del documento al vault (paso manual, después del PR)

**Esta parte NO la ejecuta esta skill.** Se describe aquí por completitud:

Cuando el PR está aprobado y fusionado, una persona ejecuta (fuera del agente):

```bash
cd ai-architecture/ai-platform-plan/05-runbooks/migracion/
bash scripts/aplicar-migracion.sh --apply
```

(O usando el patrón específico en `create-runbook/staging/` si hay documentos nuevos listos.)

Este script:
- Valida que el documento en staging cumpla todos los checks.
- Copia el documento de staging a `Runbooks/<Category>/RB-NNN <Title>.md` del clon del repositorio del vault (`MEMORY_VAULT_ROOT`).
- Actualiza el índice en el vault.
- No confirma ni abre otro PR: deja los cambios en el árbol de trabajo.

Luego la persona hace `git add`, `git commit`, `git push` manualmente.

### 9. Memoria (minuto 14–15)

1. Ejecuta `mem_save` con:
   - `type: architecture`
   - `topic_key: runbook/RB-NNN`
   - `project: <service>` (el `service` del frontmatter)
   - Contenido: síntomas, causa raíz, refs de Jira, fecha de creación, nombre de quien lo redactó.

## Herramientas y cómo se usan

| Herramienta | Paso | Notas |
|---|---|---|
| `jira_get_issue` | 1 | Lee ticket con `comment_limit: 20` para contexto y decisiones previas |
| `mem_search`, `search`, `search_by_metadata` | 2 | Busca runbooks existentes; no escribe |
| `next-runbook-id.sh` | 3 | Script local; escanea el vault y devuelve `RB-NNN` libre (bash 3.2 compatible) |
| `instantiate-runbook.sh` | 4 | Script local; instancia la plantilla con los defaults de `patterns/<pattern>.md` en `$MEMORY_PROJECT_STAGING/create-runbook/`, **nunca en el vault** (rechaza con exit 3 si `--out-dir` resuelve dentro de él) |
| `Edit` (staging) | 4 | Rellena a mano las variables del diagnóstico que el script deja pendientes en el cuerpo |
| `AskUserQuestion` | 5 | Obtiene confirmación de diagnóstico |
| `validate-runbook.sh` | 6 | Script local; 12 checks contra plantilla; exit 0 o 1 |
| `open-pr` | 7 | Delega; abre el PR en el repositorio del vault, con el dueño como revisor |
| `mem_save` | 9 | Registra en memory bajo `runbook/RB-NNN` |

## El hook `guard-vault-write.sh` y por qué existe

```bash
case "$path" in
  "$root"/*/90\ -\ Memory/*) exit 0 ;;
  "$root"/*)
    echo "guard-vault-write: ... curated vault content; ... only '90 - Memory/' ... (D-11)" >&2
    exit 2 ;;
esac
```

**Por qué:** Cada vault (un repositorio propio, clonado bajo `~/Projects/Vaults/<slug>/`) es un corpus curado por personas, no por agentes. La única excepción es `90 - Memory/`, donde el puente automático de memory escribe. Cualquier otra ruta requiere un PR humano revisado. Ver ADR-011 (D-11).

**Implicación para esta skill:** Todos los runbooks, índices y patrones van en staging y se aplican vía PR humana. El hook no es un problema que "burlar" o "eludir": es una política de calidad curada. Esta skill la respeta.

## Ruta de staging

Por defecto, `$HOME/.local/share/knowledge/staging/create-runbook/`. Se controla con `MEMORY_PROJECT_STAGING`.

```bash
mkdir -p "$MEMORY_PROJECT_STAGING/create-runbook"
echo "# RB-001: ..." > "$MEMORY_PROJECT_STAGING/create-runbook/RB-001 Title.md"
```

## Patrón elegido para Runbooks (ver migracion/README.md)

Esta skill sigue el patrón consolidado en `ai-platform-plan/05-runbooks/migracion/`:

1. **Redacción en staging.** El agente prepara documentos fuera del vault.
2. **Validación completa.** Los scripts (`next-runbook-id.sh`, `validate-runbook.sh`) corren antes de cualquier aplicación.
3. **Confirmación humana.** Una persona revisa y ejecuta `aplicar-migracion.sh --apply`.
4. **PR auditado.** La cadena final (push + PR) queda como un cambio humano.

No hay atajo: es el precio de un vault curado.
