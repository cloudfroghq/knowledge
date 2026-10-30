# Checklist — create-runbook

- [ ] `jira_get_issue(KEY, comment_limit=20)` corrido; síntomas, país, partner e `issue_type` extraídos.
- [ ] `mem_search(incident/<KEY>)`, `search(query=<síntomas>)` y `search_by_metadata(type="runbook")` sin encontrar un runbook existente que valga; si lo hay, validado que el procedimiento sigue siendo válido.
- [ ] `next-runbook-id.sh` ejecutado; `RB-NNN` anotado y no duplicado en el vault.
- [ ] `instantiate-runbook.sh --id RB-NNN --pattern <pattern> --out-dir "$MEMORY_PROJECT_STAGING/create-runbook"` corrido; documento en `RB-NNN <Title>.md` (FUERA del vault); frontmatter completo con `status: draft`, síntomas y referencias, causa raíz verificada contra el ticket, pasos de resolución válidos hoy, sin PII.
- [ ] Confirmación humana obtenida con `AskUserQuestion`; ajustes incorporados si los hubo.
- [ ] `validate-runbook.sh <archivo-en-staging>` exit 0; los 12 checks pasados.
- [ ] Índice (`Runbooks.md`) actualizado en staging con la nueva fila si aplica.
- [ ] `Issue Patterns from Jira.md` actualizado en staging si el `pattern` es nuevo.
- [ ] Delegación a `open-pr` completada; PR abierto contra `main` de `knowledge` en rama `docs/rb-nnn-<slug>`, con título `docs(runbooks): add RB-NNN <slug>` y descripción coherente.
- [ ] `mem_save` (`type: architecture`, `topic_key: runbook/RB-NNN`) ejecutado con síntomas, causa raíz, refs de Jira, servicio y owner.

**Paso manual posterior (NO en esta skill):**

- [ ] PR revisado y fusionado contra `main` de `knowledge` (decisión humana).
- [ ] Persona ejecuta `bash ai-architecture/ai-platform-plan/05-runbooks/migracion/scripts/aplicar-migracion.sh --apply` para copiar el documento de staging al vault.
- [ ] Cambios en el vault confirmados y pusheados (sin abrir otro PR desde el agente).
