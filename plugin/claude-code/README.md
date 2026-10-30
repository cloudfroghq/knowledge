# Plugin `knowledge` para Claude Code

Sirve el servidor MCP de conocimiento curado sobre tus vaults locales por stdio. Claude Code lo arranca con `uvx`, que instala el paquete `cloudfrog-knowledge` desde el tag del repositorio que nombra `.mcp.json`. Se instala desde el marketplace `cloudfroghq`:

```bash
claude plugin marketplace add cloudfroghq/claude-plugins
claude plugin install knowledge@cloudfroghq --scope user
```

La guía completa por equipo, con prerrequisitos y precalentado de la caché, está en [`docs/install.md`](../../docs/install.md).

## Qué trae

| Pieza | Qué hace |
|---|---|
| `.mcp.json` | Servidor `knowledge`: `uvx --managed-python --python 3.12 --from git+ssh://git@github.com/cloudfroghq/knowledge.git@v<versión> cloudfrog-knowledge serve --transport stdio`. Sus tools llegan como `mcp__plugin_knowledge_knowledge__<tool>` y el servidor aparece en `/mcp` como `plugin:knowledge:knowledge`. |
| `hooks/hooks.json` | `SessionStart` que corre `cloudfrog-knowledge doctor --brief` con el mismo paquete y las mismas opciones que el servidor. No imprime nada si todo está bien; si algo falla, una sola línea de hasta 200 bytes, sin contar el salto de línea (el tope de `doctor --brief`). También habla, con el doctor sano, cuando un vault servido declara `Contexts/config.json` y esta máquina no lo inicializó: `knowledge doctor: init pending for vault(s) <slugs>; run: cloudfrog-knowledge init`. Si el doctor no contesta en 8 segundos (caché de `uv` fría) también calla. Si `uvx` falla sin que el doctor diga nada (sin red, o un tag que el remoto no tiene), avisa una vez por día y por versión del plugin, no en cada arranque. Lo único que el hook escribe en disco es la marca de ese aviso (ver Diagnóstico). Con el doctor sano, lanza además `cloudfrog-knowledge jira sync --auto` en segundo plano, desprendido del hook (no lo espera ni muestra su salida): sincroniza los vaults que declaran `jira`, tienen sus variables y ya cumplieron `sync.throttle_minutes`, y escribe sus tareas en el memory local y el cursor junto al índice. `KNOWLEDGE_JIRA_AUTOSYNC=0` lo apaga (`docs/runbooks/sincronizar-jira.md`). |
| `skills/search-first` | Protocolo de lectura: `search` con campos de puntero y `get_document` de una sola sección. |
| `skills/repair-mcp-tool` | Reparación de una tool rota con su test de regresión en rojo primero. |
| `skills/code-question` | Pregunta conceptual sobre código sin un identificador conocido: `jg` (jevgrep) sobre una carpeta estrecha con `--max-source-bytes 20000`, nunca sobre un vault; conteos, símbolos e impacto quedan en `rg` y graphify. |

## Opciones (`userConfig`)

| Opción | Variable que recibe el servidor | Vacía |
|---|---|---|
| `vaults_config` (archivo) | `KNOWLEDGE_VAULTS` | El servidor usa `~/.config/knowledge/vaults.yaml` (o `$XDG_CONFIG_HOME/knowledge/vaults.yaml`). |
| `embeddings_provider` (`openai`, `none` o `bag-of-words`) | `KNOWLEDGE_EMBEDDINGS_PROVIDER` | Decide el `vaults.yaml` (por omisión `openai`). |

Claude Code exporta una opción sin valor como cadena vacía, y el servidor trata una cadena vacía como variable ausente: dejar las dos opciones vacías es el caso normal.

Para guardarlas: `/plugin` → `Installed` → `knowledge` → `Configure` dentro de la sesión, o `claude plugin configure knowledge@cloudfroghq --values-stdin < valores.json`. Después hay que reiniciar Claude Code.

## Lo que el plugin no define, y por qué

- **`KNOWLEDGE_HOME`**: gana sobre `data_dir` del `vaults.yaml`. Apuntarlo al directorio de datos del plugin (`${CLAUDE_PLUGIN_DATA}`) haría que desinstalar o reinstalar el plugin borrara los índices y obligara a embeber de nuevo todos los vaults. Los índices viven donde diga tu `vaults.yaml` (`data_dir`) o, sin eso, en `~/.local/share/knowledge`.
- **`OPENAI_API_KEY`**: el servidor la hereda del entorno con el que arranca Claude Code. Si el `.mcp.json` la mapeara desde una opción sin valor, Claude Code exportaría una cadena vacía encima de la variable heredada y el arranque caería con `E_EMBEDDINGS_KEY_MISSING`. Si tu sesión no hereda la clave, `cloudfrog-knowledge doctor` lo dice con ese mismo código.

## Variables de entorno opcionales

| Variable | Para qué |
|---|---|
| `UVX_BIN` | Ruta absoluta de `uvx` cuando Claude Code no hereda tu `PATH` (por ejemplo, abierto desde el Dock). Por omisión, `uvx` resuelto por `PATH`. |
| `KNOWLEDGE_DOCTOR_BUDGET` | Segundos que el hook espera al doctor (por omisión 8). |
| `XDG_CACHE_HOME` | Base de la marca del aviso diario del hook (`knowledge/session-start-package-failed`). Por omisión, `$HOME/.cache`. |
| `MCP_TIMEOUT` | Milisegundos que Claude Code espera el arranque de un servidor MCP. El primer arranque con la caché fría puede pasar de los 30 s por omisión: `60000` da margen. |

## Diagnóstico

```bash
uvx --managed-python --python 3.12 --from 'git+ssh://git@github.com/cloudfroghq/knowledge.git@v0.8.0' cloudfrog-knowledge doctor
```

Imprime un JSON con la regla de descubrimiento que ganó, el archivo de registro, por vault el directorio, el proveedor y si su clave está definida (nunca el valor), si `KNOWLEDGE_HOME` se puede escribir y las versiones del paquete y del contrato. Sale con 0 si todo está bien, 1 con problemas y 2 si la configuración no carga.

El hook de inicio de sesión no repite el mismo fallo en cada arranque. Cuando `uvx` no logra ni arrancar el paquete (sin red, o un tag que el remoto no tiene), imprime `knowledge: server package failed (uvx rc N); diagnose: …` una vez por día y por versión del plugin. La marca es el archivo de una línea `${XDG_CACHE_HOME:-$HOME/.cache}/knowledge/session-start-package-failed`: borrarlo repite el aviso en el siguiente arranque, y si no se puede escribir, el aviso sale en todos. Reintentar con `uvx --offline` no sirve: con uv 0.10 un tag git se consulta en el remoto incluso con esa bandera. Mientras tanto, `/mcp` muestra el servidor como `failed`.

## Versión

`version` de `plugin.json`, el `@v<versión>` de `.mcp.json` y `version` de `pyproject.toml` (más el proyecto en `uv.lock`) se mueven juntos con `scripts/bump-version.sh X.Y.Z` y se comprueban con `scripts/check-version.sh [vX.Y.Z]`. El tag se corta después de mergear el pull request de la release, sobre ese merge.
