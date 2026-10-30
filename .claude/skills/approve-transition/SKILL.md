---
name: approve-transition
description: Emite el token humano que destraba, para esta sesión, una transición de Jira que `guard-jira-transition.sh` bloqueó por no ser de avance reversible.
when_to_use: cuando `jira_transition_issue` fue bloqueado por `guard-jira-transition.sh` y una persona, fuera del agente, decidió que la transición debe avanzar.
allowed-tools: ["Read", "Bash(.claude/skills/approve-transition/write-approval.sh *)"]
disable-model-invocation: true
user-invocable: true
argument-hint: "<clave-ticket> <id-transición>"
---

# approve-transition

Contraparte humana de `.claude/hooks/guard-jira-transition.sh` (ADR-031). El hook deja pasar sin aprobación las transiciones reversibles de avance (`61` In Analysis, `111` In Develop, `121` In Code Review); para cualquier otra transición — intermedia o terminal, según la clasificación de ADR-031 — exige que la variable `CD_JIRA_APPROVED_TRANSITION` coincida con el id pedido, sin token no hay avance. Esta skill es la única vía legítima para fijar esa variable con respaldo de una decisión humana, y por eso `disable-model-invocation: true`: el modelo nunca la invoca por su cuenta, solo la persona.

## Pasos

1. Confirmar, fuera del agente, que la transición debe aplicarse — la decisión de negocio, no la ejecución, es lo que aprueba una persona.
2. Ejecutar `.claude/skills/approve-transition/write-approval.sh <clave-ticket> <id-transición>`. El script rechaza los ids `61`, `111` y `121` (no necesitan aprobación) y, para el resto, escribe un token en `$CD_APPROVALS_DIR/<clave-ticket>-<id-transición>.json` (por defecto `~/.knowledge/approvals/`) con quién y cuándo aprobó, y un TTL de 30 minutos.
3. Exportar `CD_JIRA_APPROVED_TRANSITION=<id-transición>` en la misma sesión, tal como indica la salida del script — es la condición que `guard-jira-transition.sh` verifica para dejar pasar la llamada.
4. Invocar `jira_transition_issue` con ese id. El hook compara `CD_JIRA_APPROVED_TRANSITION` contra el `transition_id` de la llamada y, si coinciden, deja pasar.
5. `mem_save` dejando constancia de quién aprobó, cuándo, el ticket y la transición.

## Cuándo NO usarla

- Para `61`, `111` o `121`: el hook ya las permite sin este paso.
- Para redactar o publicar una respuesta a Triara o al cliente: eso lo hace directamente una persona; esta skill solo destraba la transición de Jira (ADR-031).
