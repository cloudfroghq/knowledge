# Checklist — open-pr

- [ ] Rama de origen confirmada (`git branch --show-current`); no es un spike sin fusión.
- [ ] Commits de la rama en Conventional Commits, sin prefijo de ticket, atómicos.
- [ ] `.claude/rules/git.md` consultado; destino de merge confirmado para este tipo de cambio, no asumido.
- [ ] `bkt pr list --state OPEN --json --jq '…select(.source.branch.name == "<rama>")'` corrido; ningún PR ya abierto desde esta rama.
- [ ] Cuerpo del PR redactado (desde `PULL_REQUEST_TEMPLATE.md` del repo o, si aún no existe, la misma estructura de secciones), sin texto de relleno genérico.
- [ ] `git push -u origin <rama>` confirmado antes de crear el PR.
- [ ] `bkt pr create --target <destino> --title "…" --description "…"` ejecutado con `--target` explícito (nunca omitido ni como argumento posicional).
- [ ] URL del PR enlazada al ticket con `jira_create_remote_issue_link`.
- [ ] Transición `121` (In Code Review) aplicada con `jira_transition_issue`.
- [ ] `mem_save` completado con la URL del PR y el destino usado.
