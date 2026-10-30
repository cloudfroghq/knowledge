# Configuración de servidores MCP en ámbito de usuario

Este repositorio, los paquetes de contexto que cada vault guarda en `Contexts/`, y los
checkouts de servicio que los enlazan o copian, ya NO declaran servidores MCP compartidos en su
`.mcp.json`. Ahí solo queda lo que de verdad varía por repositorio: en este,
`graphify`, porque la ruta de su grafo es relativa al checkout, y `memory`,
porque el perfil de tools y el proyecto se deciden por repositorio.

Todo lo demás —`cloudfrog-knowledge`, `mcp-atlassian`, `newrelic-community`—
vive en el ámbito de usuario de Claude Code (`~/.claude.json`, sección
`mcpServers`), alimentado por variables de entorno. Así se evita declarar el
mismo servidor una vez por contexto y el conflicto de ámbitos que aparece cuando el mismo
nombre de servidor se define a la vez en ámbito de proyecto y de usuario con
contenidos distintos.

`datadog-mcp` no aparece aquí: la instancia de ámbito de usuario ya autentica
por OAuth contra Datadog y no necesita variables ni claves de API.

## Variables de entorno requeridas

Ninguna vive en este repositorio ni en ningún `.mcp.json` versionado. Cada
persona las define en un fichero fuera de cualquier checkout, con permisos
`600`, cargado desde la configuración de su shell.

| Variable | Para qué sirve |
|---|---|
| `CD_KNOWLEDGE_URL` | URL del MCP de este servidor que usa el servidor `cloudfrog-knowledge`: `https://<despliegue>/mcp`. Cada despliegue la documenta en su vault, en `Deploy/`. |
| `CD_KNOWLEDGE_TOKEN` | Token bearer contra ese mismo MCP: la `MCP_API_KEY` del despliegue (solo lectura) o el bearer personal que te asignen. |
| `ATLASSIAN_EMAIL` | Correo de la cuenta de Atlassian usada por `mcp-atlassian` (Jira y Confluence). |
| `ATLASSIAN_API_TOKEN` | API token de esa cuenta de Atlassian. |
| `NEW_RELIC_API_KEY` | API key de New Relic para `newrelic-community`. |
| `NEW_RELIC_ACCOUNT_ID` | Id de cuenta de New Relic asociado a esa API key. |

## Plantilla del fichero de entorno

Copia esto a un fichero fuera de cualquier repositorio (por ejemplo,
`~/.config/mcp/mcp-env.fish`), rellena los valores reales, y déjalo con
permisos `600`:

```fish
# Variables que consumen los servidores MCP de ámbito de usuario.
# Contiene credenciales: permisos 600, fuera de cualquier repositorio.
set -gx CD_KNOWLEDGE_URL "https://TU-HOST-DEL-MCP-DE-CONOCIMIENTO/mcp"
set -gx CD_KNOWLEDGE_TOKEN "TU_TOKEN_AQUI"
set -gx ATLASSIAN_EMAIL "tu-correo@tuempresa.com"
set -gx ATLASSIAN_API_TOKEN "TU_API_TOKEN_DE_ATLASSIAN_AQUI"
set -gx NEW_RELIC_API_KEY "TU_API_KEY_DE_NEW_RELIC_AQUI"
set -gx NEW_RELIC_ACCOUNT_ID "TU_ACCOUNT_ID_DE_NEW_RELIC_AQUI"
```

Y cárgalo desde `~/.config/fish/config.fish` (adapta la ruta y el shell si no
usas fish):

```fish
if test -r ~/.config/mcp/mcp-env.fish
    source ~/.config/mcp/mcp-env.fish
end
```

## Servidores a declarar en ámbito de usuario

En `~/.claude.json`, dentro de `mcpServers` (ámbito de usuario: se aplica en
cualquier repositorio, no se versiona con el proyecto). Ajusta `JIRA_URL` y
`CONFLUENCE_URL` al dominio real de tu organización en Atlassian.
El `CONFLUENCE_URL` de `mcp-atlassian` termina en `/wiki`; el de `cloudfrog-knowledge` (`sync_confluence`, `confluence export`) es la raíz del sitio, sin `/wiki`, y no se copia de aquí.

```json
{
  "cloudfrog-knowledge": {
    "type": "http",
    "url": "${CD_KNOWLEDGE_URL}",
    "headers": { "Authorization": "Bearer ${CD_KNOWLEDGE_TOKEN}" }
  },
  "mcp-atlassian": {
    "type": "stdio",
    "command": "uvx",
    "args": ["--python=3.12", "mcp-atlassian"],
    "env": {
      "JIRA_URL": "https://TU-DOMINIO.atlassian.net",
      "JIRA_USERNAME": "${ATLASSIAN_EMAIL}",
      "JIRA_API_TOKEN": "${ATLASSIAN_API_TOKEN}",
      "CONFLUENCE_URL": "https://TU-DOMINIO.atlassian.net/wiki",
      "CONFLUENCE_USERNAME": "${ATLASSIAN_EMAIL}",
      "CONFLUENCE_API_TOKEN": "${ATLASSIAN_API_TOKEN}"
    }
  },
  "newrelic-community": {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@piekstras/newrelic-mcp-server"],
    "env": {
      "NEW_RELIC_API_KEY": "${NEW_RELIC_API_KEY}",
      "NEW_RELIC_ACCOUNT_ID": "${NEW_RELIC_ACCOUNT_ID}"
    }
  }
}
```

El pin `--python=3.12` en `mcp-atlassian` es deliberado: se declara siempre, nunca
`uvx` a secas.

## `memory` sigue siendo por repositorio, con el perfil `projects`

Las tools `mem_*` llegan por dos caminos que no se solapan. El plugin
`memory@cloudfroghq` de Claude Code (sus tools llegan como
`mcp__plugin_memory_memory__*`; el plugin viejo `cloudfrog@cloudfrog` usa un
prefijo retirado y migrar a `memory@cloudfroghq` es lo recomendado, ver `docs/install.md`)
aporta la memoria de sesión —`mem_save`, `mem_search`, `mem_context`,
`mem_get_observation` y el resto del perfil `agent`—, se instala desde el
marketplace `cloudfroghq` (`docs/install.md`) y no necesita declararse en ningún
`.mcp.json`. El servidor `memory`
que sí declara este repositorio aporta los diez del perfil `projects`:
`mem_project_card`, `mem_project_upsert`, `mem_task_upsert`, `mem_task_list`,
`mem_task_link`, `mem_evidence_add`, `mem_evidence_list`,
`mem_runbook_index_sync`, `mem_runbook_find` y `mem_context_pack`. Ninguno de
esos diez existe en el plugin, así que declarar el servidor no duplica nada.

```json
"memory": {
  "type": "stdio",
  "command": "${MEMORY_BIN:-memory}",
  "args": ["mcp", "--tools=projects"],
  "env": { "MEMORY_PROJECT": "knowledge" }
}
```

El perfil es `projects` y nada más. Un `tools/list` por perfil lo mide:
`agent` (18) y `admin` (4) son exactamente las 22 tools del plugin, y `all` (39)
las contiene todas, así que cualquiera de esos tres monta la misma escritura
bajo dos servidores. `projects` (10) y `workspace` (10) no repiten ninguna, pero
de los dos solo `projects` trae lo que las skills de este repositorio invocan.
Enumera el perfil con un `tools/list` real antes de ampliarlo.

`MEMORY_PROJECT` se fija a `knowledge` porque ese es el proyecto que el plugin
resuelve desde el remoto de git en este checkout. Si el servidor apuntara a otro
nombre, plugin y servidor escribirían en proyectos distintos y el historial
quedaría partido en dos. `mem_current_project` responde qué proyecto está
activo.

`MEMORY_BIN` acepta un override; su default es el nombre `memory`, que `PATH`
resuelve. No se pone una ruta absoluta: un symlink colgado hacia un checkout
que ya no está pasa cualquier `test -e` y luego mata el servidor con un `ENOENT`
opaco.

## Verificación tras montar esto

```bash
claude mcp list
```

Debe conectar sin avisos de variables ausentes y sin "conflicting scopes",
tanto dentro de un repositorio con el `.mcp.json` reducido como fuera de
cualquier repositorio.

Para `memory`, la comprobación que vale es el handshake, no un `--version`:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | "${MEMORY_BIN:-memory}" mcp --tools=projects
```

`tools/list` debe devolver exactamente las diez tools del perfil `projects`.
