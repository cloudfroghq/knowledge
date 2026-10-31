# Promover páginas de Confluence al vault

Runbook para quien mantiene un vault cuyas convenciones nacen en Confluence. El servidor no escribe en ningún vault (D-11, `docs/adr/ADR-044.md`): `sync_confluence` solo informa qué está desactualizado y `cloudfrog-knowledge confluence export` escribe las páginas fuera del vault. El contenido entra al vault por un PR humano con el dueño como revisor.

## 1. Credenciales

El vault las declara en su `vault.yaml`, por nombre de variable, nunca por valor:

```yaml
integrations:
  confluence:
    site: https://acme.atlassian.net
    secrets: { email_env: ACME_CONFLUENCE_EMAIL, token_env: ACME_CONFLUENCE_TOKEN }
```

Lo que `integrations.confluence` no declara sale de `CONFLUENCE_URL`, `CONFLUENCE_EMAIL` y `CONFLUENCE_TOKEN`. Un valor con forma de credencial dentro de `vault.yaml` se rechaza con `E_SECRET_IN_MANIFEST`.

`site` y `CONFLUENCE_URL` son la raíz del sitio, `https://<org>.atlassian.net`, **sin** `/wiki`: el cliente añade `/wiki/api/v2/pages/<id>` por su cuenta. Con `/wiki` al final el servidor pide `/wiki/wiki/api/v2/...`, cada página responde 404 y `confluence export` (igual que `sync_confluence`) las deja todas en `errors`, sin escribir ninguna. La URL que usa `mcp-atlassian` (`https://<org>.atlassian.net/wiki`) no sirve aquí.

El vault es contenido curado, así que solo puede apuntar a variables de Confluence que sean suyas: `site` debe ser `https` en `*.atlassian.net` o en un host que el operador liste en `KNOWLEDGE_INTEGRATION_SITES` (`http` solo en `127.0.0.1`, `localhost` o `::1`), y `email_env` y `token_env` deben estar en mayúsculas, llevar `CONFLUENCE` como segmento, terminar en `_EMAIL` y `_TOKEN` y empezar con el `slug` del vault en mayúsculas (`ACME_CONFLUENCE_TOKEN` para `acme`). Solo el vault por defecto puede usar los nombres sin prefijo (`CONFLUENCE_TOKEN`), y un vault que no es el por defecto y declara `site` debe nombrar las dos variables. Cualquier otro nombre, como `MCP_ADMIN_KEY`, `OPENAI_API_KEY` o el de otro vault, detiene el arranque y `confluence export` con `E_MANIFEST_INVALID` y la ruta de la clave, antes de leer ninguna variable: así ningún secreto del servidor ni de otro vault viaja al sitio que declara un vault.

## 2. Ver qué está desactualizado

Desde un cliente MCP, `sync_confluence()` (con `dry_run=True`, el valor por omisión) lista en `needs_update` los archivos bajo `scope_prefix` (`Conventions/` por omisión) cuya página es más nueva. `sync_confluence(dry_run=False)` responde `E_WRITE_FENCED` y no lee ni escribe nada.

## 3. Exportar fuera del vault

```bash
cloudfrog-knowledge confluence export --vault acme --out ~/confluence-export/acme --dry-run
cloudfrog-knowledge confluence export --vault acme --out ~/confluence-export/acme
```

- Cada archivo desactualizado queda en `<out>/<ruta relativa al vault>`, con el frontmatter del vault y `last_updated` del día en que cambió la página.
- Un `--out` dentro de cualquier vault servido, o un archivo que resuelva dentro de uno por un symlink, termina con `E_WRITE_FENCED` (código 2) sin escribir.
- Sin credenciales, `E_SECRET_MISSING` nombra las variables que faltan (código 2).
- La salida es un JSON con `written`, `unchanged` y `errors`; con algún error el código es 1. Una página que no se puede leer, convertir o escribir queda en `errors` como `{confluence_id, file, error}` y las demás se exportan igual.
- Un fallo del sistema de archivos fuera de una página (leer el vault, por ejemplo) termina con `E_EXPORT_FAILED` (código 2) y una sola línea en stderr.

## 4. Promover por PR

1. En el checkout del repositorio del vault: `git switch -c docs/confluence-<fecha>`.
2. Copia los archivos de `written` desde `<out>` a la misma ruta del vault y revisa el diff: el cuerpo viene de una conversión básica de HTML a Markdown, así que tablas y macros pueden necesitar retoque.
3. `cloudfrog-knowledge vault-lint <vault>` sin errores nuevos.
4. Commit, push y PR con el dueño del vault como revisor. Al mergear, el webhook del vault reindexa.
