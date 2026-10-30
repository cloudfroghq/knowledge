---
name: open-pr
description: Abre el pull request de la rama actual con `bkt`, arma la descripción desde la plantilla del repo, enlaza el ticket de Jira y avanza la transición a In Code Review.
when_to_use: cuando el trabajo de una rama de feature, fix, hotfix o refactor está listo para revisión y esa rama todavía no tiene PR abierto.
allowed-tools: ["Read", "Write(/tmp/pr-body-*.md)", "Bash(git status *)", "Bash(git log *)", "Bash(git branch --show-current)", "Bash(git push *)", "Bash(bkt pr create *)", "Bash(bkt pr list *)", "mcp__mcp-atlassian__jira_get_issue", "mcp__mcp-atlassian__jira_create_remote_issue_link", "mcp__mcp-atlassian__jira_transition_issue", "mcp__plugin_memory_memory__mem_save"]
disable-model-invocation: false
user-invocable: true
argument-hint: "[clave-ticket]"
---

# open-pr

Abre el pull request de Bitbucket de la rama actual con `bkt`, arma su descripción desde la plantilla del repo, enlaza el ticket de Jira correspondiente y deja el ticket en `In Code Review`.

## Pasos

1. `git branch --show-current` y `git log` para confirmar la rama de origen: que sea de un tipo que se fusiona (no un `chore/spike-*`, que nunca se fusiona) y que sus commits sean Conventional Commits, sin prefijo de ticket y atómicos.
2. Abrir `.claude/rules/git.md` y ubicar, en la tabla "Branch patterns and merge destinations", la fila cuyo "Work type" corresponde al cambio hecho en esta rama. Usar el valor de su columna "Merge destination" como destino real de `bkt pr create` — nunca asumir `master` por defecto: hay repos que mantienen varias series en paralelo, donde el destino depende de a cuál serie afecta el cambio, y tipos de cambio cuyo destino es la rama padre de un feature compuesto, no el default del repo.
3. `bkt pr list --state OPEN --limit 50 --json --jq '.pull_requests[] | select(.source.branch.name == "<rama-actual>")'` para confirmar que todavía no existe un PR abierto desde esta rama — `bkt pr list` no filtra por rama de origen, el filtro se hace sobre su salida JSON. Si ya existe uno, no se crea un segundo.
4. Componer el cuerpo del PR: si `PULL_REQUEST_TEMPLATE.md` existe en la raíz del repo, seguir sus secciones tal cual; si el repo todavía no lo tiene materializado, usar la misma estructura de secciones — Ticket, Tipo de cambio y ruta, Resumen, Cambios, Cómo probar, Evidencia, Spec y ADR, Riesgos y rollback, Checklist — con contenido real de esta rama, sin texto de relleno genérico. Guardar el resultado en un archivo temporal, por ejemplo `/tmp/pr-body-<clave-ticket>.md`.
5. El título del PR es el mensaje del primer commit convencional de la rama.
6. `git push -u origin <rama-actual>`.
7. `bkt pr create --target <destino> --title "<título>" --description "$(bat -p /tmp/pr-body-<clave-ticket>.md)"` — el destino va siempre explícito en `--target` (nunca como argumento posicional): omitirlo hace que `bkt` use el branch por defecto del remoto, que no siempre es el destino correcto. La rama de origen es la actual por defecto y se puede fijar con `--source`. `bkt pr create` no tiene `--body-file`: la descripción larga entra por sustitución de comando.
8. `jira_create_remote_issue_link` con la URL del PR que devolvió `bkt pr create`, sobre el ticket indicado en el argumento o encontrado en el nombre de la rama.
9. `jira_transition_issue` con `transition_id: "121"` (In Code Review) — permitida sin aprobación humana por `guard-jira-transition.sh` (ADR-031). Cualquier otra transición queda fuera de esta skill.
10. `mem_save` con topic key `jira-implementer/<clave-ticket>/pr` (dentro de un flujo de `jira-implementer`) o `project/<slug>/pr/<clave-ticket>` (uso manual), con la URL del PR y el destino usado.

## Cuándo NO usarla

- Si la rama es un spike (`chore/spike-*`): esas nunca se fusionan; se descartan después de guardar los hallazgos en memory.
- Si el destino real es la rama padre de un feature compuesto (subfeature): el PR se abre igual con esta skill, pero el destino leído en el paso 2 es esa rama padre, nunca el default del repo.
- Si la transición que corresponde no es `121` (el ticket ya avanzó a una etapa posterior): usar `jira_transition_issue` directamente o, si el id es terminal o irreversible, la skill `approve-transition`.
