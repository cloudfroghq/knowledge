# Checklist — sync-runbook-index

- [ ] `mem_project_card` corrido ANTES del sync; `runbooks` y `runbooks_stale` anotados.
- [ ] `search_by_metadata(type="runbook")` enumeró el vault leyendo `items` página por página, hasta que `offset + len(items)` alcanzó `total`.
- [ ] `get_document(..., full_metadata=true)` corrido por cada resultado; ningún campo del frontmatter inventado ni deducido del nombre del archivo.
- [ ] `get_stale_docs(type="runbook")` cruzado por `file_path`, nunca por título.
- [ ] `prune_missing=true` solo si la enumeración cubrió el vault completo.
- [ ] `skipped[]` leído entero y clasificado: `template` y `not_runbook` se ignoran; `invalid_id`, `invalid_status`, `unknown_service` y `missing_service` quedan como defecto del vault con su `file_path`.
- [ ] Ningún descarte se "arregló" editando el vault desde la sesión; los que lo merecen salen por PR con `open-pr`.
- [ ] `mem_runbook_find` verificado con un síntoma conocido; `id`, `stale` y `age_days` contrastados contra el frontmatter del documento.
- [ ] `mem_project_card` corrido DESPUÉS; el conteo cambió como se esperaba o se explica por qué no.
- [ ] `mem_save` (`type: discovery`, `topic_key: knowledge-mcp/runbooks/index`) si algún descarte reveló un defecto del vault.
