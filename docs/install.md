# Instalación por equipo

Tres perfiles, una línea cada uno. Todos salen del marketplace público `cloudfroghq` (`cloudfroghq/claude-plugins`):

| Plugin | Qué sirve | Prefijo de sus tools | Para quién |
|---|---|---|---|
| `knowledge@cloudfroghq` | Este servidor por stdio, lanzado con `uvx` desde el tag del repositorio público | `mcp__plugin_knowledge_knowledge__` | Quien tiene sus vaults en disco |
| `knowledge-remote@cloudfroghq` | El servidor HTTP del despliegue de tu organización | `mcp__plugin_knowledge-remote_knowledge__` | Quien no tiene acceso al código |
| `memory@cloudfroghq` | Memoria de sesión (binario `memory`) | `mcp__plugin_memory_memory__` | Todos |

`knowledge` y `knowledge-remote` no se instalan juntos: expondrían el mismo catálogo dos veces.

## Prerrequisitos

- Claude Code 2.1.224 o posterior (`claude --version`).
- Para `knowledge` (stdio):
  - `uv` (`brew install uv`) con un CPython de `uv`: el plugin lanza el servidor con `uvx --managed-python --python 3.12`, que descarga un Python 3.12 gestionado la primera vez si no lo tienes. Hace falta porque `sqlite-vec`, la extensión que ordena los vectores, solo carga en un Python con extensiones habilitadas, y el de python.org para macOS no las trae. Sin ella el servidor sigue funcionando: ordena los vectores con la biblioteca estándar (`vector_backend: python` en `doctor` y `/health`, unos 110 ms por consulta en lugar de unos 10) y da el mismo resultado; solo cambia la velocidad. `KNOWLEDGE_REQUIRE_SQLITE_VEC=1` convierte ese respaldo en el error `E_SQLITE_VEC_UNAVAILABLE`. `uvx` tiene que resolverse desde el entorno con el que arranca Claude Code. Si lo abres desde el Dock y no hereda tu `PATH`, define `UVX_BIN` con la ruta absoluta (`/opt/homebrew/bin/uvx`).
  - `bash` en el `PATH` para el hook de inicio de sesión del plugin (`#!/usr/bin/env bash`): el tope de 8 segundos del doctor necesita control de trabajos sin terminal, que `dash` (el `/bin/sh` de Debian y Ubuntu) no da. macOS y las distribuciones de escritorio lo traen; Alpine no lo trae por omisión (`apk add bash`). Sin `bash` el hook no arranca, pero el servidor MCP no depende de él: solo se pierde el aviso de `doctor --brief`.
  - Acceso a `github.com` por HTTPS: `uvx` trae el tag del repositorio público por `git+https`, sin llave SSH ni credenciales.
  - Un registro de vaults en `~/.config/knowledge/vaults.yaml` (o el archivo que elijas en la opción `vaults_config`). El formato está en [`runbooks/servir-varios-vaults.md`](runbooks/servir-varios-vaults.md).
  - `OPENAI_API_KEY` en el entorno de la sesión si algún vault usa el proveedor `openai`. Una variable definida solo en la configuración de tu shell no llega a Claude Code abierto desde el Dock; ahí hace falta `launchctl setenv` (o un LaunchAgent que lo haga al iniciar sesión), o el proveedor `none`.
- Para `memory`: el binario `memory` en el `PATH` (abajo, con Homebrew o sin él).

## Con acceso al código (stdio con tus vaults)

```bash
claude plugin marketplace add cloudfroghq/claude-plugins && claude plugin install knowledge@cloudfroghq --scope user && claude plugin install memory@cloudfroghq --scope user && uvx --managed-python --python 3.12 --from 'git+https://github.com/cloudfroghq/knowledge.git@v0.9.0' cloudfrog-knowledge doctor && memory setup claude-code
```

- El `uvx … doctor` del final **precalienta la caché** de `uv` (el primer arranque clona el tag, descarga unos 15 MB de dependencias y, si hace falta, el Python gestionado, y puede tardar más que los 30 s que Claude Code espera por omisión a un servidor MCP) y diagnostica la instalación: sale con 0 si todo está bien.
- Añade `"MCP_TIMEOUT": "60000"` al bloque `env` de `~/.claude/settings.json` como red de seguridad para el primer arranque.
- `memory setup claude-code` regenera los permisos de las tools de memory.

Comprobación: `/mcp` muestra `plugin:knowledge:knowledge` y `plugin:memory:memory` conectados.

## Sin acceso al código (HTTP con `knowledge-remote`)

`knowledge-remote` se conecta al servidor HTTP que despliega tu organización: exige que sirva por TLS y entregue un bearer por persona. La URL del endpoint MCP (`https://<despliegue>/mcp`), el bearer y los alcances de cada despliegue se documentan en su vault, en `Deploy/`. Mientras el despliegue no sirva por TLS ni entregue un bearer por persona, no se reparte ningún bearer.

```bash
export CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1 && claude plugin marketplace add cloudfroghq/claude-plugins && brew install cloudfroghq/tap/memory && claude plugin install memory@cloudfroghq --scope user && claude plugin install knowledge-remote@cloudfroghq --scope user && memory setup claude-code
```

- `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1` hace que Claude Code clone el marketplace por HTTPS: sin llave SSH de GitHub es obligatorio, también para las actualizaciones. Déjalo exportado en tu shell.
- Sin Homebrew, en lugar de `brew install cloudfroghq/tap/memory` (macOS arm64; en Linux cambia `darwin_arm64` por `linux_amd64`):

  ```bash
  gh release download -R cloudfroghq/memory-releases -p 'memory_*_darwin_arm64.tar.gz' -p checksums.txt && shasum -a 256 -c checksums.txt --ignore-missing && tar xzf memory_*_darwin_arm64.tar.gz memory && install -m 0755 memory /usr/local/bin/memory && memory --version
  ```

  El repositorio es público: `gh release download` no exige iniciar sesión, y `curl -LO` a las mismas URLs también sirve.
- Después, dentro de Claude Code: `/plugin` → `Installed` → `knowledge-remote` → `Configure`, con la URL `https://<despliegue>/mcp` y tu bearer personal. Fuera de la sesión: `claude plugin configure knowledge-remote@cloudfroghq --values-stdin < valores.json`. No uses `claude plugin install … --config` para el bearer: queda en el historial del shell. Reinicia Claude Code para aplicarlo.
- Sin plugin, el mismo servidor se registra a mano: `claude mcp add -s user -t http knowledge https://<despliegue>/mcp -H 'Authorization: Bearer <bearer>'`.

Comprobación: `curl -s https://<despliegue>/health` responde `status: ok` con `contract_version`, y `/mcp` dentro de Claude Code muestra `plugin:knowledge-remote:knowledge` y `plugin:memory:memory` conectados.

## Instalar el CLI

`init`, `doctor`, `vault-lint` y el resto de los subcomandos se corren desde tu terminal. El mismo paquete se publica por tres canales, siempre con la misma versión:

| Canal | Instalar una vez | Correr sin instalar |
|---|---|---|
| PyPI (`cloudfrog-knowledge`) | `uv tool install --managed-python --python 3.12 cloudfrog-knowledge` | `uvx --managed-python --python 3.12 cloudfrog-knowledge <subcomando>` |
| npm (`@cloudfrog/knowledge`) | `npm install -g @cloudfrog/knowledge` | `npx @cloudfrog/knowledge <subcomando>` o `pnpm dlx @cloudfrog/knowledge <subcomando>` |
| Tag de git | `uv tool install --managed-python --python 3.12 git+https://github.com/cloudfroghq/knowledge.git@v0.9.0` | `uvx --managed-python --python 3.12 --from git+https://github.com/cloudfroghq/knowledge.git@v0.9.0 cloudfrog-knowledge <subcomando>` |

- Cada instalación deja dos comandos que son el mismo programa: `knowledge` y `cloudfrog-knowledge`. El uso (`--help`), `--version` y las pistas de `doctor` e `init` nombran el que corriste; `KNOWLEDGE_COMMAND_LABEL` sigue mandando sobre ambos (ver el README, «Etiqueta del comando en las pistas»).
- `--managed-python --python 3.12`: el paquete declara `requires-python >=3.12,<3.13`, pero `uv` ignora el tope al elegir intérprete y, sin `--python`, usaría el primer Python más nuevo que encuentre. El CPython gestionado por `uv` además carga `sqlite-vec` (ver Prerrequisitos).
- El paquete de npm no trae Python: en la primera corrida descarga `uv` 0.10.4 de su release en GitHub, comprueba su SHA-256 contra la suma que viene dentro del paquete, lo guarda en `~/.cache/cloudfrog-knowledge/uv/` (`%LOCALAPPDATA%` en Windows) y corre `uv tool run --managed-python --python 3.12 --from cloudfrog-knowledge==<versión> <comando>`. Funciona en macOS (arm64, x64), Linux (x64, arm64; glibc o musl) y Windows (x64, arm64). Detrás de un proxy que Node no atraviesa, apunta `CLOUDFROG_KNOWLEDGE_UV` a un `uv` que ya tengas. Un fallo del paquete antes de arrancar el CLI imprime `<código>: <mensaje>` en stderr y sale con 2.
- Fija la versión cuando la uses desde un script o un `.mcp.json`: `cloudfrog-knowledge==0.9.0` (o `uvx … cloudfrog-knowledge@0.9.0`), `npx @cloudfrog/knowledge@0.9.0` o el tag `@v0.9.0`.

Comprobación: `knowledge --version` (o `npx @cloudfrog/knowledge --version`) imprime `knowledge 0.9.0`; `knowledge doctor` diagnostica la instalación con tus vaults.

## Inicializar el cliente

Un vault que declara `Contexts/config.json` trae el paquete de contexto de cada aplicación (`Contexts/<app>/`: `CLAUDE.md`, `.mcp.json` y `.claude/`). Enlazarlo en tus checkouts es trabajo de tu máquina, nunca del servidor: un despliegue remoto no ve tu disco. Con tus vaults ya registrados (`~/.config/knowledge/vaults.yaml`):

```bash
cloudfrog-knowledge init --dry-run --repo <id> && cloudfrog-knowledge init --repo <id>
```

Cada máquina elige en qué checkouts instala un paquete: la lista `apply` de su `repos.local.json`, con los identificadores del registro (`Contexts/config.json`). El registro es el mismo para todos; la selección es tuya.

- `--repo ID` agrega `ID` a `apply` (repetible; `--repo SLUG:ID` lo limita a un vault). Puedes seleccionar un checkout que todavía no clonas: `init` lo instala cuando aparezca.
- `--all` agrega cada checkout presente en esta máquina que tiene paquete.
- `--drop ID` lo quita de `apply` y borra solo los enlaces de ese checkout que apuntan a `Contexts/<paquete>/`; tus archivos, tus enlaces propios, una copia (modo `copy`) y las entradas de ignorado se quedan.
- Sin bandera, `init` instala lo que ya dice `apply`. En una máquina nueva (sin `apply` y sin paquetes instalados) te pregunta en una terminal qué checkouts quieres (`all`, `none`, una lista de ids, o vacío para decidir después); con `--yes` o sin terminal no instala nada y te lista los checkouts presentes con la pista `--repo ID`.
- La primera corrida de esta versión sobre un `repos.local.json` sin `apply` lo crea con los checkouts que ya tienen su paquete instalado (enlaces hacia `Contexts/<paquete>/`, o la copia que hizo `init` en modo `copy`): tu configuración no cambia.
- Un id que el registro ya no declara sale de `apply` con un aviso. `--dry-run` muestra los cambios de la selección (`would add`, `would remove`) sin escribir nada.
- `apply` solo decide dónde se instalan los paquetes: los objetivos del grafo y el servidor leen el registro sin mirarla.

- Para cada vault con `Contexts/config.json`, ubica cada raíz: `--root NOMBRE=DIR` (o `--root SLUG:NOMBRE=DIR` para un solo vault), tus variables `KNOWLEDGE_<RAÍZ>_ROOT`, tu `repos.local.json`, el `default` que sugiera el vault o, si nada de eso existe, los checkouts de git cuyo `origin` coincide con un remoto declarado bajo `~/Projects` (o bajo cada `--search-dir`). Si no encuentra una raíz te la pregunta; con `--yes` la salta.
- Escribe `~/.config/knowledge/<slug>/repos.local.json` (bajo `$XDG_CONFIG_HOME` si la tienes definida), enlaza el paquete de cada aplicación seleccionada cuyo checkout tienes, reporta como `not-selected` los checkouts presentes que no seleccionaste y salta los que no tienes, sin error.
- Termina diciendo dónde vive cada archivo: `vaults.yaml`, cada `repos.local.json` y cada checkout enlazado. Edítalos y vuelve a correr `init`: una segunda corrida sin cambios no escribe nada.
- Solo instala en un checkout cuyo `origin` es el remoto declarado: si no se puede leer (sin remoto, sin `.git`) queda `unverified`, `init` sale con 1 y no instala salvo con `--force`; si es otro repositorio, es un error. El `path` de una aplicación nunca sale de su raíz (`..` es `E_CONTEXTS_INVALID`).
- Nunca escribe dentro de un vault. Si un checkout ya tiene su propio `CLAUDE.md` o `.mcp.json` sin versionar (también un enlace simbólico tuyo), `init` no los toca y sale con 1: `--force` los aparta como `<nombre>.pre-ai-context.bak-<epoch>`, y una copia (modo `copy`) que se desvió del paquete se resincroniza con `--refresh`.
- `link` es el único modo recomendado: el paquete vive en el vault y cada checkout lo ve tal cual. `copy` sigue funcionando para un vault que todavía lo declara, pero ningún caso lo exige.
- Un `.claude` que en el checkout es un **directorio real** (Claude Code crea sus worktrees en `<checkout>/.claude/worktrees/` y no acepta un `.claude` enlazado) sigue siendo real: `init` enlaza dentro de él cada entrada del `.claude` del paquete (`agents`, `commands`, `skills`, `settings.json`…) y nunca toca lo que el paquete no trae (`settings.local.json`, `agent-memory/`, `worktrees/`). Una entrada real sin versionar es un conflicto que `--force` aparta dentro del mismo `.claude`; un enlace que `init` hizo hacia un vault que se movió se reenlaza solo. Las entradas de ignorado nombran cada enlace (`/.claude/agents`), nunca el directorio real.
- Lo que el checkout **versiona en git** es suyo: un `CLAUDE.md` o `.mcp.json` versionado, o una entrada de `.claude/` versionada, se conserva con el aviso «owned by the checkout», sin conflicto y sin cambiar el código de salida. Un `.claude/<colección>/` versionado (por ejemplo `.claude/skills/` con una skill del repositorio) recibe además las entradas del paquete que le faltan; más abajo, una skill o un agente es una unidad y no se mezcla.

`cloudfrog-knowledge` en estos ejemplos es cualquiera de las formas de «Instalar el CLI»: `knowledge`, `uvx … cloudfrog-knowledge` o `npx @cloudfrog/knowledge`.

Un vault que no tiene aplicaciones que enlazar declara `"repos": {"roots": [], "entries": []}` (y `"graph": {"targets": []}`) en su `Contexts/config.json`: `init` lo reporta como `no-apps`, no escribe nada y sale 0, y `doctor` no lo cuenta como `init pending`. Un `roots` o `entries` que no sea un arreglo, o una entrada mal formada, sigue siendo `E_CONTEXTS_INVALID`; el registro vacío solo es válido en la declaración de un vault.

En modo `link`, Claude Code escribe en el vault a través del `.claude` enlazado (`settings.local.json`, `agent-memory/`, `worktrees/`): el `.gitignore` del vault tiene que cubrir `Contexts/*/.claude/settings.local.json`, `Contexts/*/.claude/agent-memory/` y `Contexts/*/.claude/worktrees/`. Si el vault es un repositorio git y no los cubre, `init` avisa con `W_CONTEXTS_GITIGNORE` (no bloquea).

Comprobación: `cloudfrog-knowledge doctor --brief` deja de decir `init pending for vault(s) …`, y el hook de inicio de sesión del plugin vuelve a callar.

En una máquina que consume un servidor remoto (sin índice, sin clave de embeddings, sin bearers propios), `cloudfrog-knowledge doctor --client` revisa solo lo tuyo: que el vault exista y se lea, que su `Contexts/config.json` cargue, tu `repos.local.json` y el estado de cada checkout (`init --dry-run`, sin escanear ni preguntar). Un checkout presente que no seleccionaste es `not-selected` (información, no un problema); uno seleccionado sin su paquete cuenta como `init` pendiente (código 0); uno con enlaces a su paquete que no está en `apply` es el aviso `W_CONTEXTS_LINKED_NOT_SELECTED` (código 0: agrégalo con `--repo ID` o quita sus enlaces con `--drop ID`), y un `repos.local.json` sin `apply` es el aviso `W_CONTEXTS_SELECTION_MISSING` (código 0: el próximo `init` la crea). Un checkout que es un repositorio git sin remoto `origin` es un aviso (`W_CONTEXTS_CHECKOUT_UNVERIFIED`, código 0); un conflicto (`E_CONTEXTS_CONFLICT`) o un checkout que no es el declarado (`E_CONTEXTS_CHECKOUT_INVALID`) sale con 1. `--brief` funciona igual.

Un vault que envuelve `init` y `doctor` en sus propios scripts (por ejemplo `Contexts/scripts/init.sh` y `Contexts/scripts/doctor.sh`, que fijan la versión del paquete y el vault) fija en ellos `KNOWLEDGE_COMMAND_LABEL='Contexts/scripts/{command}.sh'`: las pistas de `init` y `doctor` nombran esos scripts y no este CLI. Ver el README, «Etiqueta del comando en las pistas».

## Bearers y alcances de un despliegue HTTP

Cada bearer de un despliegue HTTP tiene alcances: `read` usa el servidor y `admin` además puede llamar `reindex`, `sync_confluence` y `POST /webhook/reindex` con bearer manual. Un bearer sin `admin` recibe `E_SCOPE_REQUIRED` en esas tools (403 en el webhook); las demás responden igual. Por stdio no hay bearers y nada se comprueba.

| Variable o clave | Qué hace |
|---|---|
| `MCP_API_KEY` | Sin `MCP_ADMIN_KEY`, tiene `read` y `admin`, igual que en 1.2.0. Con ella, solo `read`. |
| `MCP_ADMIN_KEY` | Bearer con `read` y `admin`. Debe ser distinta de `MCP_API_KEY` (si coinciden, el arranque sale con `E_MANIFEST_INVALID`). |
| `bearers` en `vaults.yaml` | Un bearer por persona (`id`, `token_env`, `scopes`); sustituye a `MCP_API_KEY` y `MCP_ADMIN_KEY`: con `bearers` declarado, esas dos variables se ignoran. Ver `docs/runbooks/servir-varios-vaults.md`, sección 5. |

El bearer que pones en `Configure` de `knowledge-remote` (o en `claude mcp add … -H 'Authorization: Bearer <bearer>'`) es de solo lectura salvo que quien administra el despliegue te dé uno con `admin`. `cloudfrog-knowledge doctor` muestra el bloque `auth` del servidor: ids, alcances y nombres de variables, nunca un token.

## Memory-bridge: publicar un vault en el memory local

Un vault que declara `memory-bridge` en `features` (y `memory.project` en su `vault.yaml`) publica su proyecto en el servidor de memory que corre en tu máquina: la card (`cloudfrog-knowledge memory-bridge sync --card`) y el índice de runbooks (`memory-bridge sync`, y cada `reindex` por stdio). Solo funciona por stdio: un despliegue HTTP no carga la extensión (`W_EXTENSION_UNAVAILABLE_HTTP`).

| Variable | Qué hace |
|---|---|
| `KNOWLEDGE_MEMORY_URL` | Dónde escucha memory; por omisión `http://127.0.0.1:6366`. Solo `http` en loopback: otra URL se rechaza con `E_MEMORY_URL_NOT_LOOPBACK`. |
| `MEMORY_HTTP_TOKEN` | El bearer que se envía en cada escritura. Sin él no se escribe nada (`E_MEMORY_TOKEN_MISSING`). |
| `KNOWLEDGE_MEMORY_REQUIRE_TOKEN` | Con exactamente `0`, las escrituras salen sin bearer. |
| `MEMORY_BIN` | El binario que corre `memory-bridge promote`; por omisión, `memory` en el `PATH`. |

Excepción local: el `memory serve` de una persona corre hoy sin `MEMORY_HTTP_TOKEN` (las integraciones de opencode, pi y obsidian todavía no envían bearer), así que su `requireAuth` acepta escrituras sin él. Para ese memory, activa el bridge con `KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0` en el entorno del servidor knowledge, nunca en un despliegue:

```bash
KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0 uv run cloudfrog-knowledge memory-bridge sync --vault acme --card
```

El servidor stdio también lee esa variable: sin ella y sin `MEMORY_HTTP_TOKEN`, arranca con `W_EXTENSION_UNCONFIGURED` para `memory-bridge`. Cuando `memory serve` corra con token, exporta el mismo `MEMORY_HTTP_TOKEN` en los dos procesos y quita la excepción.

Comprobación: `curl -s http://127.0.0.1:6366/projects/<memory.project>/runbooks | jq '.total'` cuenta los runbooks que memory indexó.

## Jira: issues como tareas de memory

Un vault que declara `jira` en `features`, con `memory.project` e `integrations.jira` en su `vault.yaml`, copia los issues de sus proyectos de Jira como tareas de su proyecto en el memory local, una por issue y por `jira_key`. Solo por stdio: un despliegue HTTP no carga la extensión (`W_EXTENSION_UNAVAILABLE_HTTP`) y `sync_jira` no aparece en su catálogo.

Solo los vaults que lo declaran sincronizan Jira: sin el bloque, el vault no entra en `jira sync --auto` ni en `sync_jira`. Para activarlo en un vault, en un PR de ese vault:

1. añade `jira` a `features`, `memory.project` e `integrations.jira` (`site`, `projects` y `secrets`) a su `vault.yaml`, con los nombres de variable del propio vault: `<SLUG>_JIRA_EMAIL` y `<SLUG>_JIRA_TOKEN`;
2. exporta esas dos variables en el entorno de tu sesión (el correo de tu cuenta de Jira y un API token suyo).

```yaml
features: [jira, memory-bridge]
memory:
  project: acme
integrations:
  jira:
    site: https://acme.atlassian.net
    projects: [ACME]
    secrets: {email_env: ACME_JIRA_EMAIL, token_env: ACME_JIRA_TOKEN}
```

| Variable | Qué hace |
|---|---|
| `<PREFIJO>_JIRA_EMAIL` / `<PREFIJO>_JIRA_TOKEN` | El correo de tu cuenta de Jira y un API token suyo (id.atlassian.com → Security → API tokens), con los nombres que declara `integrations.jira.secrets`. Exportados en el entorno de tu sesión, nunca en un archivo del repo. Sin ellas, el servidor stdio arranca con `W_EXTENSION_UNCONFIGURED` y `sync_jira` responde `E_SECRET_MISSING` con sus nombres. |
| `MEMORY_HTTP_TOKEN` / `KNOWLEDGE_MEMORY_REQUIRE_TOKEN` | Las mismas del memory-bridge: la escritura de tareas exige el bearer salvo `KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0` para el memory local sin token. Un `--dry-run` no los necesita. |
| `KNOWLEDGE_JIRA_AUTOSYNC` | Con `0`, el hook de sesión del plugin no lanza la sincronización en segundo plano. |

Antes de la primera corrida, crea la card del proyecto con su `jira_project` (sin ella, memory crea una card mínima con un `jira_project` de relleno):

```bash
KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0 uv run cloudfrog-knowledge memory-bridge sync --vault acme --card
uv run cloudfrog-knowledge jira sync --vault acme --dry-run --since -30d
KNOWLEDGE_MEMORY_REQUIRE_TOKEN=0 uv run cloudfrog-knowledge jira sync --vault acme
```

Con el plugin, cada sesión que arranca con `doctor` sano lanza `cloudfrog-knowledge jira sync --auto` en segundo plano, desprendido del hook: sincroniza los vaults que declaran `jira`, tienen sus variables exportadas y ya cumplieron `integrations.jira.sync.throttle_minutes` (60 por omisión). El procedimiento completo, la forma del reporte y qué hacer con cada código están en `docs/runbooks/sincronizar-jira.md`.

## Juez de `search` (rerank)

Un vault que declara un bloque `rerank` en su `vault.yaml` reordena los resultados de `search` con un juez: Jev, de TypeSafe (`provider: jev`), o Kev, un servidor local en loopback (`provider: kev`). El bloque es contenido curado y entra por PR humano; nombra la variable de la clave, nunca la clave:

```yaml
rerank:
  provider: jev
  default: on                    # search reordena sin que el llamador lo pida; off: solo con rerank="on"
  model: jev-1.13.0              # versión fija: jev-latest se rechaza
  key_env: ACME_TYPESAFE_API_KEY    # TYPESAFE_API_KEY solo en el vault por defecto
  candidates: 10                 # documentos que lee el juez, 1-12
  units_per_doc: 2               # bloques por documento, 1-3
  timeout_ms: 1500
  max_input_tokens: 20000
  max_usd_per_day: 1.00          # tope por vault y por día; 0 sin tope
  conditional_gap: null
  never_send: []                 # prefijos de ruta que nunca salen de la máquina, p. ej. ["Personal/"]
```

| Variable | Qué es |
|---|---|
| `TYPESAFE_API_KEY` / `<PREFIJO>_TYPESAFE_API_KEY` | La clave de TypeSafe, con el nombre que declara `rerank.key_env`. Exportada en el entorno de la sesión (o del servicio en Dokploy), nunca en un archivo del repo. Abierto desde el Dock, Claude Code no hereda tu shell: hace falta `launchctl setenv`. Sin ella el servidor arranca igual, por stdio y por HTTP, con `W_RERANK_KEY_MISSING`, y ese vault busca sin juez. |
| `KNOWLEDGE_RERANK` | `off` apaga el juez de todos los vaults del proceso. |
| `KNOWLEDGE_RERANK_PROVIDERS` | Proveedores permitidos (`jev,kev` por omisión). |
| `KNOWLEDGE_RERANK_BASE_URL` | Reemplaza la URL de todo juez: `https://api.typesafe.ai` o un `http` en loopback. |
| `KNOWLEDGE_RERANK_TIMEOUT_MS` | Plazo total de cada llamada al juez (200 a 60.000 ms). |

Un vault que declara el juez nombra en su `vault.yaml` una variable con el prefijo de su slug, válida sea o no el vault por defecto del proceso. Para que busque con juez, exporta en el entorno de la sesión (o del servicio) esa variable; varios vaults pueden llevar la misma clave bajo nombres distintos. En fish: `set -Ux ACME_TYPESAFE_API_KEY $TYPESAFE_API_KEY`. Cómo llega la clave a cada despliegue se documenta en su vault, en `Deploy/`.

Costo: unos 11.700 tokens de entrada por búsqueda reordenada a 0,042 USD por millón, ≈0,0005 USD; 1.000 búsquedas ≈0,49 USD. Las consultas repetidas salen de la caché del proceso sin costo. El contenido viaja a `api.typesafe.ai` solo desde un vault que declara `jev`, nunca desde `90 - Memory/` ni con `include_memories=true`.

Comprobación: `cloudfrog-knowledge doctor` muestra por vault un bloque `rerank` con `key: ok` (o `missing:<VARIABLE>`), y `doctor --brief` termina en `rerank <slug>:jev/on`. Una `search` reordenada trae `rerank_score` en sus filas y `_meta["knowledge/rerank"].applied: true`.

## Con el plugin `cloudfrog@cloudfrog` instalado

El plugin de memory se llama `memory@cloudfroghq` y sus tools llevan el prefijo `mcp__plugin_memory_memory__`, el único que listan los `allowed-tools` de las skills de este repositorio y de sus paquetes de contexto. El plugin viejo `cloudfrog@cloudfrog` (id `cloudfrog:memory`) las expone con el prefijo retirado `mcp__plugin_cloudfrog_memory__`: en una máquina que lo conserve, esas skills siguen funcionando, pero cada llamada a memory pide permiso, salvo que `~/.claude/settings.json` todavía permita las entradas viejas. Migrar es lo recomendado; el comando de abajo deja instalado `memory@cloudfroghq`.

```bash
claude plugin uninstall cloudfrog@cloudfrog && claude plugin marketplace remove cloudfrog && claude plugin marketplace add cloudfroghq/claude-plugins && claude plugin install memory@cloudfroghq --scope user && memory setup claude-code
```

`marketplace remove` borra también las opciones guardadas del plugin `cloudfrog@cloudfrog`: revisa con `claude plugin configure memory@cloudfroghq` cuáles quedaron sin valor. Después quita a mano de `permissions.allow` en `~/.claude/settings.json` las entradas `mcp__plugin_cloudfrog_memory__*`, que ya no corresponden a ninguna tool.

## Revisar un vault para su PR

`cloudfrog-knowledge vault-lint <ruta-al-vault>` revisa un vault sin escribir nada. Con las reglas `1.3` (las de por omisión) exige un `last_updated` ISO en cada nota, avisa de un `type` fuera del conjunto cerrado, rechaza un `scope` que no sea el `slug` del vault, `global` ni uno de los `scopes:` de su `vault.yaml`, exige rutas NFC sin choques de mayúsculas, comprueba que cada runbook (`type: runbook` bajo `Runbooks/`, o el `runbooks.dir` del manifiesto) pueda indexarlo el servidor de memory, y no cuenta como wikilink lo que está dentro de código. `--rules 1.2` aplica solo lo que cuenta `reindex.validation`. El `index.exclude` del manifiesto saca archivos de la regla de stems repetidos, de los enlaces de origen y de las reglas de frontmatter (`last_updated`, `type`, `scope`): es lo que permite que `Contexts/*/CLAUDE.md`, que no son notas, no necesiten frontmatter (`index: {exclude: ["Contexts/**"]}`). Las reglas `1.2` no leen el manifiesto.

`--fix` escribe, y nunca en un vault que esta instalación sirve (`KNOWLEDGE_VAULTS`, tu `vaults.yaml` o `VAULT_PATH`): sobre uno sale con código 2 y `E_WRITE_FENCED` sin tocar nada. Copia el vault con su `.git`, corre `--fix` en la copia y entrega el cambio por PR del vault. `--out DIR` hace la copia por ti (sin `.git`, para comparar con `diff -ru`).

```bash
cp -R <ruta-al-vault> /tmp/vault-fix && cd /tmp/vault-fix
cloudfrog-knowledge vault-lint . --fix > /tmp/vault-fix.json   # fixed, remaining, errors, warnings
git diff > /tmp/vault-fix.patch                                # solo líneas del frontmatter
```

`--fix` toma la fecha de `updated`, `last_review` o `last_verified.date` (en ese orden) y la deja como `last_updated`; el resto de un mapa `last_verified` queda como `verified_against`. Lo que no tiene una fecha de la cual partir sale en `remaining` con su motivo y se escribe a mano.

## Actualizar

```bash
claude plugin marketplace update cloudfroghq && claude plugin update memory@cloudfroghq && claude plugin update knowledge@cloudfroghq
```

Con `knowledge-remote`, actualiza `knowledge-remote@cloudfroghq` en lugar de `knowledge@cloudfroghq`. Después, `/reload-plugins` dentro de la sesión, y `brew upgrade memory` para el binario.

## Diagnóstico

- `knowledge` (stdio): `uvx --managed-python --python 3.12 --from 'git+https://github.com/cloudfroghq/knowledge.git@v0.9.0' cloudfrog-knowledge doctor` imprime un JSON con la regla que eligió el registro de vaults, el estado de cada vault (directorio, proveedor, si la clave está definida sin mostrarla, si hay índice), si `KNOWLEDGE_HOME` se puede escribir y las versiones del paquete y del contrato. El hook de inicio de sesión del plugin corre la versión breve y solo habla cuando algo falla, con una línea de hasta 200 bytes sin contar el salto de línea (el tope de `doctor --brief`).
- Si un vault declara en `features` una extensión sin sus credenciales (por ejemplo `confluence` sin `CONFLUENCE_EMAIL` y `CONFLUENCE_TOKEN`, o los nombres que fije `integrations.confluence.secrets` en su `vault.yaml`), el servidor stdio arranca igual y deja `W_EXTENSION_UNCONFIGURED` en su log con los nombres de las variables que faltan; solo esa extensión responde con error.
- `CONFLUENCE_URL` es la raíz del sitio de Atlassian (`https://<org>.atlassian.net`), **sin** `/wiki`: el cliente de este servidor añade `/wiki/api/v2/...` por su cuenta. Con `/wiki` al final cada página responde 404 y `sync_confluence` y `confluence export` las cuentan todas en `errors`. La misma regla se aplica a `integrations.confluence.site` del `vault.yaml`. El `CONFLUENCE_URL` de `mcp-atlassian` es otra variable, de otro proceso, y ese sí lleva `/wiki`.
- Un servidor en estado `failed` en `/mcp` con el doctor en verde suele ser tiempo de arranque: sube `MCP_TIMEOUT` o vuelve a precalentar la caché.
- Sin red, el servidor stdio no arranca: `uvx` consulta el tag en GitHub en cada inicio.
- Si `uvx` no logra ni arrancar el paquete (sin red, o un tag que el remoto no tiene), el doctor no llega a decir nada y el hook lo avisa con `knowledge: server package failed (uvx rc N); diagnose: …` **una vez por día y por versión del plugin**, no en cada arranque. La marca es un archivo de una línea, `${XDG_CACHE_HOME:-$HOME/.cache}/knowledge/session-start-package-failed`: borrarlo hace que el aviso vuelva en el siguiente arranque, y si no se puede escribir el aviso sale en todos. Entre un aviso y otro el servidor sigue apareciendo como `failed` en `/mcp`. Reintentar con `uvx --offline` no ayuda: con uv 0.10 un tag git se consulta en el remoto incluso con esa bandera. Un doctor que no contesta en 8 segundos (caché fría) no avisa; lo hará el arranque siguiente.
