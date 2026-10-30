# Checklist — approve-transition

- [ ] La transición fue decidida por una persona, fuera del agente.
- [ ] El id de transición no es `61`, `111` ni `121` (esos no necesitan este token).
- [ ] `write-approval.sh <clave-ticket> <id-transición>` ejecutado; token creado en `$CD_APPROVALS_DIR` (o `~/.knowledge/approvals` por defecto).
- [ ] `CD_JIRA_APPROVED_TRANSITION=<id-transición>` exportado en la sesión antes de invocar `jira_transition_issue`.
- [ ] `jira_transition_issue` ejecutado; `guard-jira-transition.sh` dejó pasar la llamada.
- [ ] `mem_save` completado con quién aprobó, cuándo, ticket y transición.
