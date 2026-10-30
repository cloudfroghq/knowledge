# Servir varios vaults con un solo servidor

Runbook para quien despliega o mueve instancias de `cloudfrog-knowledge`. Un proceso puede servir un vault (modo de compatibilidad) o varios (registro `vaults.yaml`). En despliegue, cada instancia corre `infra/compose.yml` con su propio entorno: sus vaults, su índice, sus deploy keys y su secreto de webhook. Lo propio de cada despliegue (dominio, aplicación en la plataforma, claves por nombre, rollback) se documenta en su vault, en `Deploy/`.

## 1. Modos de descubrimiento

El servidor decide qué sirve en este orden; gana el primero que exista:

1. `KNOWLEDGE_VAULTS`: ruta a un `vaults.yaml` (relativa al checkout). Si el archivo no existe, el arranque falla con `E_VAULTS_FILE_MISSING`.
2. `VAULT_PATH`: modo de compatibilidad, un solo vault. Si además hay `KNOWLEDGE_VAULTS` y `VAULT_PATH` apunta a otro sitio, se avisa con `W_VAULT_PATH_IGNORED`.
3. `vault_path` en el `config.json` que nombre `KNOWLEDGE_CONFIG_JSON`: mismo modo de compatibilidad. Este repositorio no trae ningún `config.json`, porque ningún vault vive aquí: en un checkout sin `VAULT_PATH` ni `KNOWLEDGE_VAULTS` gana la regla siguiente.
4. `$XDG_CONFIG_HOME/knowledge/vaults.yaml` o `~/.config/knowledge/vaults.yaml`: solo si ninguna de las anteriores está definida. Sin este archivo tampoco, el arranque falla con `E_VAULT_PATH_MISSING`: no hay vault por omisión.

`KNOWLEDGE_VAULTS`, `VAULT_PATH`, `KNOWLEDGE_INDEX_DIR`, `CHROMA_PERSIST_DIR`, `KNOWLEDGE_HOME`, `KNOWLEDGE_CONFIG_JSON` y `KNOWLEDGE_EMBEDDINGS_PROVIDER` vacías se leen como ausentes.

## 2. Dónde vive el índice

Cada vault tiene un directorio de índice con `index.sqlite` (documentos, chunks, FTS5 y vectores) y sus sidecars: `link_graph.json`, `usage_stats.json`, `index_meta.json` y `skipped_files.json`.

- Modo de compatibilidad: `KNOWLEDGE_INDEX_DIR` > `CHROMA_PERSIST_DIR` > `KNOWLEDGE_HOME/<slug>` (solo si `KNOWLEDGE_HOME` está definida y no vacía) > `<raíz>/.data/<slug>`. El slug sale de `CHROMA_COLLECTION` (en blanco se lee como ausente); sin ella, de `chroma_collection` del `config.json`; sin él, del `slug` del `vault.yaml` del directorio (solo si es un slug válido, porque arma rutas); y sin nada de eso es `vault`, un nombre que no es de ningún tenant. Con el slug del `vault.yaml`, las variables de credencial que el vault nombra (`ACME_TYPESAFE_API_KEY` en un vault `acme`) coinciden con el vault servido. Con `CHROMA_PERSIST_DIR`, `index.sqlite` vive junto a `chroma.sqlite3`, en el mismo volumen.
- Registro: `<data_dir>/<slug>`, con `data_dir` del `vaults.yaml` o `KNOWLEDGE_HOME`; sin ninguno, `$XDG_DATA_HOME/knowledge`.
- La imagen fija `KNOWLEDGE_HOME=/data/index` y `CHROMA_PERSIST_DIR=/data/chroma`. Sin `KNOWLEDGE_INDEX_DIR` gana el segundo: el índice queda en `/data/chroma/index.sqlite`, junto a `chroma.sqlite3` y dentro del volumen que el compose monte ahí. Ese valor por omisión existe para los despliegues cuyo compose no está en este repo (ver la sección 7).
- Los composes de `infra/` fijan `KNOWLEDGE_INDEX_DIR=/data/index/<slug>` sobre el volumen `knowledge-index`. Si tu compose monta el volumen de Chroma en `/data/chroma`, es solo origen de migración: el servidor no escribe ahí.
- `cloudfrog-knowledge migrate-index` prepara el índice de cada vault sin embeber nada: copia `usage_stats.json` y los pesos aprendidos de `link_graph.json` desde el directorio donde vivía Chroma (`<data_dir>/<slug>/chroma`, `CHROMA_PERSIST_DIR` o `--from-chroma`) y deja el índice vacío; la primera tool (o el webhook) lo llena. Con `--prune-chroma` borra solo `chroma.sqlite3` y los directorios de segmentos de Chroma.
- El entrypoint de la imagen (`infra/bootstrap-vault.sh`) corre `migrate-index` sin `--build` y luego `serve`, con el reporte de la migración en stderr. Un `index.sqlite` de un paquete más nuevo detiene el arranque con `E_INDEX_VERSION_NEWER`. Embeber queda para la primera tool o el webhook: con `openai` tarda uno o dos minutos y el `HEALTHCHECK` da 20 s de arranque.
- `vector_backend` (en `doctor` y en `/health`) dice quién ordena los vectores: `sqlite-vec` o `python`. `python` significa que la extensión no carga en ese intérprete (un Python sin extensiones cargables, como el de python.org para macOS): el resultado es el mismo y cada consulta cuesta unos 110 ms en lugar de unos 10. La imagen corre sobre `python:3.12-slim-trixie` y carga la extensión; en stdio, `uvx --managed-python` usa un CPython de `uv`, que también la carga.
- No hay instantánea del índice que exportar ni importar: el índice se reconstruye desde el vault. El repositorio no trae `scripts/export-data.sh` ni `import-data.sh`.

## 3. Añadir un vault

1. Elige un `slug` que cumpla `^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$`. Nombra el directorio del índice y la colección, así que no lo cambies después.
2. Añade una entrada a `vaults.yaml` (parte de `vaults.yaml.example`). `path` es obligatorio. `deploy_key_env`, `git` y `webhook` exigen `repo`.
3. Nombra los secretos por variable de entorno (`deploy_key_env`, `webhook.secret_env`). Nunca los escribas en el archivo.
4. Si el vault trae `vault.yaml`, su `slug` debe coincidir con el del registro.
5. Valida sin arrancar nada:

```bash
uv run cloudfrog-knowledge vault-lint <ruta-al-vault>              # reglas 1.3: las del vault
uv run cloudfrog-knowledge vault-lint <ruta-al-vault> --rules 1.2  # las que cuenta reindex.validation
uv run python -c "from pathlib import Path; from knowledge.settings import parse_vaults_file; print(parse_vaults_file(Path('vaults.yaml'), check_paths=False))"
```

## 4. Cuándo se ejecuta git

Git solo corre si el vault declara un remoto. En el contenedor eso significa `VAULT_REMOTE` no vacío; en `vaults.yaml`, `repo` con `git.auto_pull` distinto de `false`.

- `VAULT_REMOTE` definido: `infra/bootstrap-vault.sh` crea o adopta el clon en `/data/vault-repo`, lo fuerza a `VAULT_BRANCH` y exige `GIT_DEPLOY_KEY_B64`. Sin clave sale con `E_DEPLOY_KEY_MISSING` (código 64) sin tocar git.
- `VAULT_REMOTE` vacío: cero llamadas a git. El vault es lo que ya contiene el volumen.
- El compose genérico guarda los clones en el volumen `knowledge-vaults` (`/data/vaults/<slug>`), de lectura y escritura. `90 - Memory` es la única carpeta donde el servidor escribe. La variante sin git, con el vault en un volumen de solo lectura, está en «Vault que llega como volumen».
- Si `VAULT_PATH` no es un directorio, el arranque sale con `E_VAULT_PATH_NOT_DIR` y nombra la ruta, el remoto y la rama.

## 5. Secretos por instancia

Cada instancia define los suyos en su entorno; ninguno se comparte entre instancias de organizaciones distintas:

| Variable | Qué es |
|---|---|
| `GIT_DEPLOY_KEY_B64` | Deploy key de solo lectura registrada en el repo vault de esa organización, en base64. Solo hace falta con `VAULT_REMOTE`. |
| `MCP_WEBHOOK_SECRET` | Secreto HMAC del webhook `push` de ese repo. Sin él, el webhook se rechaza y se avisa con `W_WEBHOOK_SECRET_MISSING`. |
| `MCP_API_KEY` | Bearer del servidor. Sin `MCP_ADMIN_KEY` tiene los alcances `read` y `admin`; con ella, solo `read`. Sin ninguno de los dos (ni `bearers`), `E_HTTP_API_KEY_MISSING`. |
| `MCP_ADMIN_KEY` | Opcional. Bearer con alcance `admin`, el único que puede llamar `reindex`, `sync_confluence` y `POST /webhook/reindex` con bearer manual. Distinta de `MCP_API_KEY`: si coinciden, `E_MANIFEST_INVALID`. |
| `OPENAI_API_KEY` | Embeddings con el proveedor `openai`. Sin ella, `E_EMBEDDINGS_KEY_MISSING`. |
| `<SLUG>_TYPESAFE_API_KEY` | Opcional. Clave del juez de `search` (rerank), con el nombre que declare el `rerank.key_env` del `vault.yaml` del vault (`ACME_TYPESAFE_API_KEY` para el slug `acme`); `TYPESAFE_API_KEY` solo vale para el vault por defecto. Si la plataforma guarda la clave bajo un nombre corto, el compose la copia al nombre del vault. Sin valor, `W_RERANK_KEY_MISSING` y ese vault busca sin juez. |
| `CONFLUENCE_URL`, `CONFLUENCE_EMAIL`, `CONFLUENCE_TOKEN` | Solo con la extensión `confluence`. `CONFLUENCE_URL` es la raíz del sitio (`https://<org>.atlassian.net`), **sin** `/wiki`: el cliente añade `/wiki/api/v2` por su cuenta, y con `/wiki` al final todas las páginas responden 404 y quedan en `errors` de `sync_confluence`. Un vault puede fijar su propio sitio y sus variables en `integrations.confluence` (`docs/runbooks/promover-confluence.md`). |

El webhook se configura en el repo vault con la URL `http://<host>:<puerto>/webhook/reindex?target=vault`. Con secreto, GitHub firma cada entrega en `X-Hub-Signature-256` (`sha256=`) y en `X-Hub-Signature` (`sha1=`); el servidor prueba primero la primera y acepta solo `sha256`, también bajo `X-Hub-Signature`, que es donde la manda Bitbucket. La ruta existe solo cuando el vault por defecto declara un repositorio (`repo` en `vaults.yaml` o `VAULT_REMOTE`): sin repositorio no hay nada que actualizar con git y el servidor responde 404 en esa ruta; el índice se reconstruye con la tool `reindex`.

El clon de un vault sigue solo su rama servida (`git.branch` en `vaults.yaml`; `VAULT_BRANCH` con `VAULT_PATH`; `main` por omisión). Una entrega firmada cuyo `ref` es otra rama o un tag, o que borra el ref (`deleted: true`, o un `after` de ceros), recibe 200 y no corre nada. Un `repo:push` de Bitbucket Cloud no trae `ref` de primer nivel: lista cada referencia que el push actualizó en `push.changes`, con su estado antes (`old`) y después (`new`, `null` si el push la borró), y la entrega corre solo si algún cambio tiene `new` de tipo `branch` con el nombre de la rama servida; si no, recibe la misma respuesta:

```json
{"status": "ignored", "mode": "signature", "reason": "not_served_branch", "ref": "refs/heads/fix/typo", "branch": "main", "target": "vault"}
```

`reason` vale `not_served_branch` o `ref_deleted`. Un merge con `--delete-branch` (GitHub) o con «Close source branch» (Bitbucket) manda dos entregas casi juntas: la de la rama servida da 202 y la del borrado, `ignored`. En un `repo:push` de Bitbucket, `ref` es la referencia del primer cambio como ref de git (`refs/heads/<rama>`, `refs/tags/<tag>`), y `ref_deleted` con `refs/heads/<rama servida>` cuando un cambio borró la rama servida. Se juzgan dos formas: un payload con `ref` de primer nivel (GitHub, Gitea, Forgejo) y uno con `push.changes` (Bitbucket Cloud); un `ping`, un cuerpo que no es JSON, cualquier otra forma, una lista `push.changes` vacía o un cambio que no se puede leer siguen disparando el pull. Un bearer manual con alcance `admin` siempre encola el reindex, traiga el cuerpo que traiga. Los runs de un vault nunca se solapan: una entrega que llega mientras corre uno suma una sola pasada al terminar (que trae lo último del remoto), y la tool `reindex` espera el mismo candado. En el historial de entregas, un 200 es una entrega ignorada a propósito y un 202, una encolada.

Cada pasada lleva el clon a la punta de `origin/<rama servida>`, también cuando la rama se reescribió (force push) o el clon divergió: `git fetch --prune origin`, luego `git merge --ff-only` si `HEAD` es ancestro de la punta remota y, si no, `git checkout -f -B <rama> origin/<rama>`, el mismo comando con el que el bootstrap adopta un clon existente (sin `git clean`: los archivos sin seguimiento se quedan). La realineación deja un `WARNING` con el commit viejo y el nuevo, y después corre el reindex. Si el `fetch` falla, el clon no se toca y el log lleva un `ERROR`. `/health` lo muestra sin leer logs:

| Clave | Valor |
|---|---|
| `vault_commit` | Commit abreviado que tiene el clon ahora (`null` sin repositorio). |
| `last_sync_at` | Fin de la última actualización en este proceso, ISO 8601 en UTC. |
| `last_sync_result` | `fast_forward`, `up_to_date`, `realigned` o `failed`. |
| `last_sync_error` | Motivo de un `failed`, en una línea y sin credenciales; `null` en los demás casos. |
| `last_change_at` | Fin de la última actualización que movió el clon en este proceso, ISO 8601 en UTC. |
| `last_change_result` | `fast_forward` o `realigned`. Un `up_to_date` o un `failed` posterior no lo reemplaza. |

Las tres `last_sync_*` valen `null` hasta la primera actualización tras un arranque (el bootstrap ya trajo el clon) y en un vault sin repositorio; las dos `last_change_*`, hasta la primera actualización que mueve el clon. Las `last_sync_*` describen el último intento y las `last_change_*`, el último cambio: dos merges seguidos dejan una pasada de más que ya no trae nada (`last_sync_result: up_to_date`), y `last_change_result` sigue diciendo cómo llegó el merge. Un `failed` no cambia `status`. Si `last_sync_result` se queda en `failed` entrega tras entrega, el clon está sirviendo el commit de `vault_commit`: revisa la deploy key y el remoto.

### Alcances por bearer

Cada bearer HTTP es un principal con alcances: `read` usa el servidor y `admin` además llama las tools de administración (`reindex`, que cambia el índice, y `sync_confluence`, que solo lee) y `POST /webhook/reindex` con bearer manual. Ninguna escribe en el vault. Un bearer sin `admin` recibe `isError` con `E_SCOPE_REQUIRED` en esas tools y 403 en el webhook; el resto de las tools no cambia.

| Configuración | Principales |
|---|---|
| Solo `MCP_API_KEY` | `service`, con `read` y `admin`: igual que en 1.2.0 |
| `MCP_ADMIN_KEY` (y opcionalmente `MCP_API_KEY`) | `admin` (`MCP_ADMIN_KEY`), con `read` y `admin`; `service-read` (`MCP_API_KEY`), solo `read` |
| `bearers` en `vaults.yaml` | Uno por entrada, y ningún otro: `MCP_API_KEY` y `MCP_ADMIN_KEY` se ignoran |

`bearers` vive solo en `vaults.yaml` (un despliegue `VAULT_PATH` no lo lee) y nombra la variable de cada token, nunca el token:

```yaml
bearers:
  - id: ana            # ^[a-z0-9][a-z0-9-]{0,31}$, único; es lo que sale en el log
    token_env: KNOWLEDGE_TOKEN_ANA
    scopes: [read]     # read por omisión; admin exige read
  - id: ops
    token_env: KNOWLEDGE_TOKEN_OPS
    scopes: [read, admin]
```

Por HTTP, una entrada cuya variable está vacía detiene el arranque con `E_SECRET_MISSING`, y dos entradas con el mismo token con `E_MANIFEST_INVALID`. Por stdio no hay bearers: se ignoran con `W_BEARERS_IGNORED_STDIO`. `cloudfrog-knowledge doctor` muestra el bloque `auth` (ids, alcances, nombres de variables y si están definidas, nunca un valor).

## 6. Gate de umbrales antes del corte

La imagen no trae `config.json`. Los umbrales de `get_stale_docs` (`review.thresholds_days`) salen, en este orden, de `vault.yaml` en la raíz del vault, de un archivo apuntado por `KNOWLEDGE_CONFIG_JSON` o de los valores internos, con `W_REVIEW_THRESHOLDS_FALLBACK` en el log de arranque.

**No cortes una instancia a su repo vault hasta que ese repo tenga `vault.yaml` con `review.thresholds_days`** (o hasta que montes un `config.json` y lo apuntes con `KNOWLEDGE_CONFIG_JSON`). Si no, `get_stale_docs` cambia de resultados sin que nada falle. Un vault que declara sus umbrales en `vault.yaml` no necesita que el compose defina `KNOWLEDGE_CONFIG_JSON`.

## 7. Corte de una instancia a su repo vault

Este es el procedimiento común; el dominio, la aplicación en la plataforma, las claves por nombre y el rollback de cada despliegue están en su vault, en `Deploy/`.

El orden importa. Si el compose nuevo se despliega con la deploy key vieja, el bootstrap falla en el `git clone` bajo `set -eu` y el contenedor queda en un bucle de reinicios; si se cambia la llave antes que el compose, falla igual el `fetch` del remoto viejo en el siguiente reinicio.

1. Antes: anota `curl -s https://<dominio>/health` y guarda `indexed`.
2. Registra una deploy key de solo lectura en el repo vault: `gh repo deploy-key list -R <org>/<vault>` la muestra con `read-only`.
3. Apaga el despliegue automático del compose en la plataforma, si lo tiene.
4. Mergea el cambio del compose a `main` (sin despliegue, porque el automático está apagado).
5. En el entorno del compose: `GIT_DEPLOY_KEY_B64` con la llave nueva, `MCP_ADMIN_KEY` (distinta de `MCP_API_KEY`) y `MCP_WEBHOOK_SECRET` (nuevo; es el secreto del webhook del repo vault). El resto queda igual.
6. Despliega y espera `/health` con `status: ok` y el `contract_version` publicado.
7. Crea el webhook `push` en el repo vault (`https://<dominio>/webhook/reindex?target=vault`, `application/json`, secreto = `MCP_WEBHOOK_SECRET`) y dispara el primer reindex con una entrega de prueba o un push. La entrega va firmada por HMAC y no lleva ningún bearer `admin`. Con `openai`, el primer reindex embebe el vault completo.
8. Después: el historial de entregas del webhook trae `202`, y `/health` devuelve `index_version: 1`, `vector_backend: sqlite-vec`, un `indexed` mayor que cero, un `last_indexed_at` nuevo, el `vault_commit` de la punta de la rama servida y `last_sync_result` distinto de `failed`. El número de `indexed` cuenta chunks.
9. Vuelve a encender el despliegue automático.

Rollback: devuelve el entorno anterior del compose, revierte el cambio del compose por PR y despliega. Antes de cambiar nada, respalda el volumen del contenedor (`docker run --rm -v <volumen>:/v -v "$PWD":/b alpine tar czf /b/vol.tgz -C /v .`): el compose nuevo reemplaza el índice viejo por el contenido del repo vault. El bootstrap hace `checkout -f` de `VAULT_BRANCH` sobre un clon que ya exista, así que un rollback al compose viejo solo arranca si esa rama conserva lo que el compose viejo espera.

#### Leer las llamadas por tool

El servidor cuenta cada llamada por tool, cliente y principal en el `usage_stats.json` del índice (`/data/index/<slug>`, volumen `knowledge-index`); el formato está en el README, sección «Telemetría de llamadas». Para leerlo, abre la terminal del servicio `knowledge-mcp` en tu plataforma (o `docker exec -it <contenedor> sh` en el host) y corre `doctor`, que solo lee:

```bash
python -m knowledge doctor | python3 -c 'import json,sys; v=[r for r in json.load(sys.stdin)["vaults"] if r["default"]][0]; print(json.dumps(v["tool_calls"], indent=1))'
```

El contenedor ya trae `VAULT_PATH` y `KNOWLEDGE_INDEX_DIR`, así que `doctor` lee el archivo del despliegue. `last_7_days.by_client` dice qué clientes llaman, con el nombre que cada uno declara en su `initialize` (un cliente nuevo aparece aquí con su propio nombre), `by_principal` con qué bearer (`service-read` es `MCP_API_KEY`, `admin` es `MCP_ADMIN_KEY`), `deprecated` cuántas llamadas recibió `search_code` y `unregistered` cuántas fueron a un nombre que el contrato no define. Ninguna clave trae argumentos ni el bearer. Un redespliegue no borra el archivo (vive en el volumen), pero sí los conteos que el proceso todavía no había escrito si el contenedor muere sin cerrar.

Un proceso que corre una versión sin esta telemetría reescribe el archivo sin la sección `tool_calls`: para volver atrás sin perderla, revierte a un commit que la incluya.

#### Desplegar una imagen en un destino cuyo compose no está aquí

Antes de desplegar una imagen de este paquete en un destino con su propio compose, revisa qué monta su contenedor: `docker inspect <contenedor> --format '{{json .Mounts}}'`. Si `/data/chroma` es un volumen y su compose no fija `KNOWLEDGE_INDEX_DIR`, el índice se escribe en `/data/chroma/index.sqlite`, dentro de ese volumen. Si `/data/chroma` no es un volumen, el índice se pierde en cada despliegue y el vault se re-embebe en cada arranque: monta un volumen ahí, o fija `KNOWLEDGE_INDEX_DIR` sobre uno, y recién entonces despliega.

#### Vault que llega como volumen

Cuando quien posee el contenido publica el vault en un volumen y el contenedor no debe correr git, el vault se monta de solo lectura y `90 - Memory`, la única carpeta donde el servidor escribe, va en un volumen aparte. Es un override de `infra/compose.yml`:

```yaml
services:
  knowledge-mcp:
    environment:
      VAULT_PATH: /data/vault
      VAULT_REMOTE: ""
      KNOWLEDGE_INDEX_DIR: /data/index/<slug>
    volumes:
      - vault-data:/data/vault:ro
      - "memory-export:/data/vault/90 - Memory"
volumes:
  vault-data:
    external: true
  memory-export:
```

Precondición: el volumen `vault-data` ya contiene la carpeta `90 - Memory`. El runtime no puede crear un punto de montaje dentro de un montaje de solo lectura, y sin ella `up` falla con «read-only file system» aunque `docker compose config` acepte el archivo. Sin `VAULT_REMOTE` git no corre y no hace falta ninguna deploy key. `sync_confluence` solo informa y `cloudfrog-knowledge confluence export` escribe fuera de todo vault (ADR-044), así que nada más necesita el vault escribible.

### Compose genérico (`infra/compose.yml`)

Es el compose sin organización: el mismo archivo sirve N instancias, y lo que las distingue es solo su entorno. No trae URLs de vaults, dominios ni slugs; no publica puertos (una plataforma enruta al puerto 8000 del contenedor, y quien usa `docker compose` a mano añade `ports:` en un override). `memory-cloud` y su `postgres` viven bajo el perfil `memory`: sin `--profile memory` (o `COMPOSE_PROFILES=memory`) corre solo el servidor de conocimiento. `MEMORY_CLOUD_IDENTITY_MODE` llega a `memory-cloud` con `grace` por omisión (acepta el `MEMORY_CLOUD_TOKEN` compartido junto a los tokens personales); con `required`, memory 1.7.0 o posterior responde `401 identity_required` a ese token en `/sync/*` y deja el dashboard solo a administradores. Cada instancia lo cambia en su entorno y con un nuevo despliegue.

Los vaults entran por el entorno. `KNOWLEDGE_VAULTS_YAML` lleva el contenido de un `vaults.yaml` (multilínea) y cada entrada es de uno de dos tipos:

| Entrada | Qué hace el entrypoint |
|---|---|
| `repo` (y `git.branch`, `main` por omisión) | Clona o actualiza el vault en `/data/vaults/<slug>` (o en su `path`, si lo trae). Corre en cada arranque y el servidor lo actualiza después con `reindex(pull=true)`. |
| solo `path` | Usa la carpeta montada tal cual; git no corre. Monta la carpeta en el contenedor (`/data/local` es un volumen vacío para eso; `KNOWLEDGE_LOCAL_DIR` puede nombrar un directorio del host). |

`infra/resolve-vaults.py` (`knowledge-resolve-vaults` en la imagen) valida el slug y el origen de cada entrada, escribe `/data/vaults.resolved.yaml` con todos los `path` absolutos y deja el resto de las claves intactas: el servidor lo valida al arrancar, con sus propios códigos. Después el entrypoint exporta `KNOWLEDGE_VAULTS` apuntando a ese archivo y descarta `VAULT_PATH` y `VAULT_REMOTE`, que la imagen fija por omisión y que de otro modo se avisarían como ignoradas.

Deploy key de un vault por ssh (base64 de la llave privada), en este orden: la variable que nombre `deploy_key_env` de la entrada, `<SLUG_EN_MAYÚSCULAS>_GIT_DEPLOY_KEY_B64` (`team-docs` → `TEAM_DOCS_GIT_DEPLOY_KEY_B64`) y `GIT_DEPLOY_KEY_B64`. Un remoto `file://`, `http(s)://` o una ruta local no necesita llave. Cada vault escribe la suya en `~/.ssh/knowledge_<slug>` (0600) y su clon la recuerda en `core.sshCommand`, así el `git pull` del servidor lee con la misma llave. El compose solo lista `GIT_DEPLOY_KEY_B64`: las llaves por vault van en el archivo opcional que nombra `KNOWLEDGE_ENV_FILE` (`infra/.env.vault-keys` por omisión, ignorado por git) o en el `environment` de un override.

- Sin `KNOWLEDGE_VAULTS_YAML` el entrypoint es el de siempre: `VAULT_PATH`, `VAULT_REMOTE`, `VAULT_BRANCH` y `GIT_DEPLOY_KEY_B64`, un solo vault.
- Si un clon existente no se puede actualizar (el remoto no responde), el arranque avisa con `W_VAULT_REFRESH_FAILED` y sirve lo que hay en disco; un clon que no se puede crear sale con `E_VAULT_CLONE_FAILED` y no sirve nada. Una llave ausente para un remoto ssh sale con `E_DEPLOY_KEY_MISSING` (código 64), antes de llamar a git.
- `/webhook/reindex` y `/health` sirven solo el vault por defecto. Los demás vaults con `repo` se actualizan al reiniciar o con `reindex(vault=<slug>, pull=true)` (bearer `admin`).

`lab/` prueba todo esto con tenants falsos: dos instancias de este compose, tres vaults (dos por `file://`, una carpeta), bearers aparte y una tabla PASS/FAIL (`lab/run.sh`, ver `lab/README.md`).

## 8. Errores y qué hacer

| Código | Acción |
|---|---|
| `E_VAULT_PATH_MISSING` | Define `VAULT_PATH` con un clon del repositorio del vault (o `vault_path` en `config.json`, o un `vaults.yaml`). Este repositorio no trae ningún vault. |
| `E_VAULT_PATH_NOT_DIR` | La ruta no existe: monta el volumen ahí, o revisa que el clon de `VAULT_REMOTE` en `VAULT_BRANCH` la contenga. |
| `E_DEPLOY_KEY_MISSING` | `VAULT_REMOTE` está definido y `GIT_DEPLOY_KEY_B64` vacío: cárgala, o vacía `VAULT_REMOTE`. |
| `E_EMBEDDINGS_KEY_MISSING` | Define la variable que nombra `key_env` (por omisión `OPENAI_API_KEY`) o usa el proveedor `none`. |
| `E_EMBEDDINGS_PROVIDER_UNKNOWN` | Usa `openai`, `none` o `bag-of-words`. |
| `E_HTTP_API_KEY_MISSING` | Define `MCP_API_KEY` (o `MCP_ADMIN_KEY`, o `bearers` en `vaults.yaml`) para el transporte HTTP. |
| `E_SCOPE_REQUIRED` | El bearer de la llamada no tiene el alcance `admin`: `reindex` y `sync_confluence` responden `isError` con este código, y `POST /webhook/reindex` con bearer manual, 403. Usa el bearer de `MCP_ADMIN_KEY` o una entrada de `bearers` con `scopes: [read, admin]`. |
| `E_HTTP_PORT_INVALID` | `MCP_HTTP_PORT` debe ser un entero válido. |
| `E_CONFIG_JSON_INVALID` | El `config.json` existe pero no se lee o no es un objeto JSON: corrígelo o quítalo. |
| `E_INDEX_PROVIDER_MISMATCH` | Aviso en el log: el índice se construyó con otro proveedor, modelo o plan de chunks, y el servidor lo vacía y re-embebe el vault completo antes de responder (con `openai`, el costo de un reindex completo). |
| `E_INDEX_VERSION_NEWER` | El `index.sqlite` lo escribió un paquete más nuevo que el instalado: actualiza `cloudfrog-knowledge` o apunta el vault a otro directorio de índice. `migrate-index` sale con código 2. |
| `E_INDEX_UNREADABLE` | El `index.sqlite` no es una base SQLite o SQLite no lo puede leer (archivo truncado o ajeno). `migrate-index` sale con código 2 y el arranque se detiene: mueve el archivo a otro lado y el índice se reconstruye vacío en la siguiente corrida. Un `link_graph.json` ilegible en el directorio de Chroma no detiene nada: se omiten los pesos aprendidos con un aviso en el log. |
| `E_SQLITE_VEC_UNAVAILABLE` | `KNOWLEDGE_REQUIRE_SQLITE_VEC=1` y la extensión `sqlite-vec` no carga en este Python: usa un Python con extensiones cargables (`uvx --managed-python`, el de la imagen) o quita la variable para aceptar el respaldo `python`, más lento. `doctor` y `/health` dicen qué `vector_backend` sirve. |
| `E_PRUNE_REFUSED` | `migrate-index --prune-chroma` no borra nada en un directorio que es o contiene un vault registrado: revisa `--from-chroma`. |
| `E_VAULTS_FILE_MISSING` | `KNOWLEDGE_VAULTS` apunta a un archivo que no existe. |
| `E_MANIFEST_INVALID` | El mensaje trae la ruta de la clave: corrige esa clave del `vaults.yaml` o del `vault.yaml`. En `integrations.<confluence|jira>`, `site` debe ser `https` en `*.atlassian.net` o en un host de `KNOWLEDGE_INTEGRATION_SITES` (`http` solo en loopback) y `secrets.email_env` y `secrets.token_env` deben nombrar una variable de esa integración con el prefijo del vault (`ACME_JIRA_EMAIL` para `acme`; sin prefijo, como `CONFLUENCE_TOKEN`, solo el vault por defecto), nunca un secreto del servidor ni la variable de otro vault. También sale cuando `MCP_ADMIN_KEY` es igual a `MCP_API_KEY` o dos entradas de `bearers` resuelven al mismo token: genera uno distinto con `openssl rand -hex 32`. |
| `E_SECRET_IN_MANIFEST` | Hay algo con forma de credencial en el archivo: bórralo, rótalo y referencia una variable de entorno. |
| `E_VAULT_SLUG_INVALID` | El slug no cumple el patrón de la sección 3. |
| `E_VAULT_SLUG_DUPLICATE` | Dos entradas con el mismo slug. |
| `E_VAULT_SLUG_MISMATCH` | El `vault.yaml` declara un slug distinto al del registro: haz que coincidan. |
| `E_VAULT_DEFAULT_AMBIGUOUS` | Más de una entrada con `default: true`. |
| `E_VAULT_DEFAULT_MISSING` | Hay varios vaults y ninguno es `default: true`. |
| `E_VAULT_MANIFEST_MISSING` | El registro exige `vault.yaml` en el vault y no está. |
| `E_VAULT_UNKNOWN` | El slug pedido no está registrado: al arrancar, o en el `vault=` de una llamada a una tool (el mensaje lista los slugs registrados). |
| `E_EVAL_UNAVAILABLE` | `eval` necesita `scripts/`, que no viaja en la imagen ni en el paquete. |
| `E_LINT_PATH_NOT_DIR` | `vault-lint` recibió una ruta que no es un directorio. |
| `E_KNOWLEDGE_HOME_NOT_WRITABLE` | `doctor` encontró que el directorio de datos (`KNOWLEDGE_HOME` o `data_dir` del `vaults.yaml`) no se puede escribir: apúntalo a un directorio escribible. |
| `E_FRONTMATTER_UNPARSEABLE` | Un frontmatter no es YAML válido: corrígelo en un PR del vault. |
| `E_FRONTMATTER_NOT_MAPPING` | El frontmatter debe ser un mapa de clave y valor. |
| `E_WIKILINK_UNRESOLVED` | Un wikilink no apunta a ningún archivo del vault. |
| `E_LINT_RULES_UNKNOWN` | `vault-lint --rules` solo acepta `1.2` (las reglas que cuenta `reindex.validation`) y `1.3` (las de `1.2` más las del vault). |
| `E_FRONTMATTER_LAST_UPDATED` | `vault-lint` (reglas 1.3): la nota no tiene `last_updated` o no es una fecha ISO, y `get_stale_docs` no puede medirle la edad. `vault-lint --fix` la toma de `updated`, `last_review` o `last_verified.date` si alguno es una fecha; el resto va a mano en un PR del vault. |
| `E_SCOPE_FOREIGN` | `vault-lint` (reglas 1.3): el `scope` de la nota no es el `slug` del vault, `global` ni uno de los `scopes` que declara su `vault.yaml`; es una convención de otro vault. Muévela allí, o declara el alcance en `scopes:` si el vault lo usa a propósito. |
| `E_PATH_CASE_COLLISION` | `vault-lint` (reglas 1.3): dos rutas, o un directorio, difieren solo por mayúsculas o por la forma Unicode del nombre, y chocan en un disco que no distingue mayúsculas o que normaliza. Renombra una. |
| `E_PATH_NOT_NFC` | `vault-lint` (reglas 1.3): la ruta no está en la forma Unicode NFC. Renómbrala con la forma NFC del mismo nombre. |
| `E_RUNBOOK_ID_INVALID` | `vault-lint` (reglas 1.3): un runbook (`type: runbook` bajo `runbooks.dir`, que no sea plantilla) sin `id` de la forma `RB-NNN`. El servidor de memory lo omite al reescanear. |
| `E_RUNBOOK_STATUS_INVALID` | `vault-lint` (reglas 1.3): el `status` de un runbook no es `draft`, `verified` ni `outdated`. |
| `E_RUNBOOK_SERVICE_UNKNOWN` | `vault-lint` (reglas 1.3): el `service` de un runbook no es un `slug` ni un alias de `Runbooks/services.json` (o del `runbooks.services_file` del manifiesto). |
| `E_SERVICES_MAP_INVALID` | `vault-lint` (reglas 1.3): `services.json` no es `{"services": [{"slug", "aliases"}]}`, o no declara ningún servicio. Con el archivo roto no se comprueba el `service` de los runbooks. |
| `E_PROFILE_UNKNOWN` | El perfil de tools pedido no existe. |
| `E_OUTPUT_SETTING_INVALID` | `KNOWLEDGE_STRUCTURED_OUTPUT` acepta solo `0` (default: solo los bloques de texto) o `1` (catálogo con `outputSchema` y respuestas con `structuredContent`); `KNOWLEDGE_TEXT_INDENT` acepta un entero de 1 a 8 (sangría del texto de cada respuesta; sin definir, JSON compacto). |
| `E_FIELD_UNKNOWN` | El `fields` de una llamada nombra una clave que la tool no tiene, o está vacío: el mensaje trae la lista permitida. |
| `E_ARGUMENT_CONFLICT` | Dos argumentos se excluyen: en `get_document`, `chunk_id` con `section`, `start_line` o `end_line`, o un `chunk_id` de otro documento (el mensaje nombra cuál); en `search_by_metadata`, `fields` con `facets=True`. |
| `E_ARGUMENT_OUT_OF_RANGE` | Un argumento está fuera de su rango: `limit` fuera de 1-50 en `search`, de 1-20 en `search_deep`, de 1-200 en `search_by_metadata`, `get_related` y `suggest_links` o de 1-500 en `get_stale_docs`, `offset` negativo en `search_by_metadata`, `max_items` fuera de 0-1000 en `get_impact`, `budget_tokens` fuera de [500, 12000] en `context_pack`, o `max_chars` menor que 1 en `get_document`. |
| `E_ARGUMENT_RETIRED` | Un parámetro que el contrato retiró y que el esquema sigue declarando para avisar en lugar de ignorarlo: `include_metadata` de `get_document`, retirado en 2.0.0. Usa `full_metadata=true` para todo el frontmatter, o `fields` sin `metadata` para dejarlo fuera. |
| `E_ARGUMENT_MISSING` | La llamada no nombra sobre qué actuar: `get_document` sin `file_path` ni `chunk_id`. Pasa el `chunk_id` de una fila de `search`. |
| `E_FILE_NOT_FOUND` | `get_document` sobre una ruta que no es un archivo del vault: toma `file_path` de una fila de `search`, relativo al vault y sin corchetes de wikilink. |
| `E_SECTION_NOT_FOUND` | `get_document(section=...)` con un heading que el documento no tiene: el mensaje lista hasta 20 de sus headings. |
| `E_FILE_NOT_IN_GRAPH` | `find_path` con un extremo que no está en el grafo de enlaces: ningún wikilink lo alcanza y no enlaza a nada, o no es un documento del vault. Toma `file_path` de una fila de `search`. |
| `E_FLOW_NOT_FOUND` | `get_flow` o `compare_flows` con un proveedor para el que el vault no tiene documento de flujo (una nota `Flow - <proveedor>.md`); `compare_flows` nombra todos los que faltan. `search(query='flow')` lista los que hay. |
| `E_SCOPE_EMPTY` | `sync_confluence` con un `scope_prefix` en el que ningún archivo tiene `confluence_id`: el alcance está mal o falta mapear las páginas. El mensaje nombra hasta 20 archivos con `confluence_id` fuera del alcance; pasa el `scope_prefix` que los contiene. |
| `E_RERANK_NOT_CONFIGURED` | `search(rerank="on")` sobre un vault cuyo `vault.yaml` no declara `rerank.provider`: el llamador pidió un juez que el vault no tiene. Omite `rerank` o pide al dueño del vault que declare el bloque `rerank` por PR; la clave nunca se declara, solo el nombre de su variable. |
| `E_EXPORT_FAILED` | `confluence export` no pudo leer el vault o preparar `--out` fuera de una página concreta: revisa permisos de las dos rutas. Una página que falla sola va en `errors` del JSON y el código es 1. |
| `E_WRITE_FENCED` | Una escritura caería dentro de un vault: `sync_confluence(dry_run=False)` siempre, `confluence export` con un `--out` (o un symlink) dentro de un vault servido, o `vault-lint --fix` sobre un vault servido (o con su `--out` dentro de uno): corre `--fix` en un checkout que no se sirve o con `--out DIR` fuera de todo vault. Usa `dry_run=True` para el reporte y `confluence export --out <dir fuera del vault>` para los archivos, y promuévelos por PR (`docs/runbooks/promover-confluence.md`). |
| `E_VAULT_CLONE_FAILED` | El entrypoint no pudo clonar un vault de `KNOWLEDGE_VAULTS_YAML`; el mensaje nombra el slug, el remoto y la rama. Las credenciales embebidas en la URL (`https://usuario:token@…`) salen como `***`, en el mensaje y en lo que git imprime; la URL real se queda en el `vaults.resolved.yaml` y en la configuración del clon, que es lo que git necesita. Revisa que el remoto responda desde el contenedor, que la rama exista y que la llave corresponda a ese repo. No se sirve nada. |
| `E_VAULTS_PATH_OVERLAP` | `KNOWLEDGE_VAULTS_YAML`: dos vaults ocupan el mismo directorio o uno está dentro del otro (`path` declarado, o `/data/vaults/<slug>` de un vault con `repo`; se comparan las rutas ya sin `..` ni enlaces simbólicos). Dale a cada vault su propio directorio. No se clona ni se sirve nada. |
| `E_CHUNK_ID_INVALID` | El `chunk_id` no tiene la forma `<doc_id>:<n>` o pasa del último heading: tómalo de una fila de `search`. |
| `E_DOC_ID_UNKNOWN` | Ningún archivo del vault tiene ese `doc_id`, ni después de releer el disco: el documento se movió o se borró; vuelve a buscar. |
| `E_DOC_ID_AMBIGUOUS` | Dos archivos del vault comparten `doc_id`: léelos por `file_path`. |
| `E_MEMORY_TOKEN_MISSING` | `context_pack(include_memories=true)` no lee memory porque `MEMORY_HTTP_TOKEN` no está definido; llega en `degraded`, la tool responde con el vault. |
| `E_MEMORY_URL_NOT_LOOPBACK` | `KNOWLEDGE_MEMORY_URL` no es `http` sobre `127.0.0.1`, `localhost` o `::1`: el cliente de memory queda apagado para que el bearer no salga de la máquina, con un aviso al abrir el vault. |
| `E_MEMORY_PROJECT_MISSING` | El vault no declara proyecto de memoria: añade `memory.project` en su entrada de `vaults.yaml` o en su `vault.yaml`. |
| `E_SECRET_MISSING` | Transporte HTTP: un vault declara en `features` una extensión que ese transporte carga (hoy, `confluence`) y faltan las variables de sus credenciales; el mensaje nombra las variables, nunca sus valores. Expórtalas (los nombres salen de `integrations.<extensión>.secrets` del `vault.yaml` o, sin ellos, `CONFLUENCE_EMAIL` y `CONFLUENCE_TOKEN`) o quita la extensión de `features`. Por stdio el servidor arranca igual y avisa con `W_EXTENSION_UNCONFIGURED`, y la llamada a `sync_confluence` sin esas variables responde este código como `isError`, con los mismos nombres. También sale cuando una entrada de `bearers` nombra un `token_env` vacío. |
| `E_MEMORY_UNAVAILABLE` | El servidor de memory rechazó la conexión, tardó más de 1 s, respondió algo distinto de 200 (un 401 si el token no coincide) o un cuerpo que no es una lista JSON; el log dice cuál, sin token ni URL. En `memory-bridge`, también una escritura que no llegó (conexión rechazada o más de 5 s; 30 s para los runbooks) o un comando `memory` que no arranca, tarda más de 30 s o no imprime JSON: instala memory o define `MEMORY_BIN`. |
| `E_MEMORY_WRITE_FAILED` | `memory-bridge` (o `reindex` de un vault con `memory-bridge`): el servidor de memory rechazó una escritura. El reporte trae `http_status` y `reason` con el código propio de memory (`invalid_field`, `vault_not_found`, `vault_scan_failed`, `payload_too_large` para un 413, `unauthorized` si el token no coincide), nunca el cuerpo de la respuesta ni el token. |
| `E_FEATURE_DISABLED` | Un comando o una tool de una extensión sobre un vault que no la declara, o una tool cuya extensión no declara ningún vault servido (`get_flow` sin `flows`, por ejemplo); el mensaje nombra los vaults que sí la declaran: añade la extensión (por ejemplo `memory-bridge`) a `features` del `vault.yaml` por PR del vault, o elige otro vault con `--vault` (o `vault=` en la tool). |
| `E_TOOL_RETIRED` | Una llamada a una tool que el contrato retiró (`search_code`, desde 2.0.0): el mensaje dice qué usar en su lugar (`graphify explain`, `graphify affected`, `rg`, `search`). El perfil `legacy` (`serve --profile legacy` o `KNOWLEDGE_PROFILE=legacy`) la sirve durante una release. |
| `E_JIRA_UNAVAILABLE` | `sync_jira` o `jira sync`: Jira no respondió con una página de issues (consulta rechazada con 400, revisa `integrations.jira.projects` y `jql_extra`; 5xx; más de 10 s; cuerpo que no es JSON; un 429 que se repite tras un reintento). No se escribe nada en memory ni avanza el cursor. Ver `docs/runbooks/sincronizar-jira.md`. |
| `E_JIRA_SYNC_STALLED` | `sync_jira` o `jira sync`: la corrida quedó truncada y todos sus issues se actualizaron en el minuto donde empieza, así que el cursor no puede avanzar (JQL compara minutos). Sube `--max-issues`, acota `integrations.jira.jql_extra` o pasa un `since` posterior. No se escribe nada en memory ni avanza el cursor. |
| `E_JIRA_AUTH_FAILED` | Jira rechazó el correo y el API token de las variables que nombra `integrations.jira.secrets` (401 o 403): genera otro token para esa cuenta o revisa que la cuenta vea el proyecto. |
| `E_KNOWLEDGE_REF_INVALID` | `memory-bridge promote stamp`: `--knowledge-ref` no es la ruta relativa al vault de un `.md` que existe en él (sin `/` inicial, sin `..`). Usa la ruta del documento que mergeó el PR, tras actualizar el checkout. |
| `E_CONTEXTS_INVALID` | `init`: el `Contexts/config.json` de un vault, o el `repos.local.json` de esta máquina para ese vault, no se puede leer o no tiene la forma del registro (`repos.roots`, `repos.entries`, o una entrada con un `path` que sale de su raíz con `..`; un registro vacío, `[]` y `[]`, es válido en un vault y significa que no declara aplicaciones); el mensaje nombra el archivo y la clave. Corrige el `Contexts/config.json` por PR del vault, o borra el `repos.local.json` y vuelve a correr `cloudfrog-knowledge init`. Los demás vaults se inicializan igual y el comando sale con 1. |
| `E_ROOT_UNKNOWN` | `init --root NOMBRE=DIR`: ningún vault que se inicializa declara la raíz `NOMBRE` en su `Contexts/config.json`. Revisa el nombre (`python3 -m knowledge.repos --vault-dir <vault> --json` lista las raíces) o usa `--root <slug>:NOMBRE=DIR`. No se escribe nada. |
| `E_CONTEXTS_REPO_UNKNOWN` | `init --repo ID` o `init --drop ID`: ningún vault que se inicializa declara el checkout `ID` en su `Contexts/config.json` (con `--repo SLUG:ID`, el vault `SLUG` no lo declara). No se escribe nada y el código es 2. Revisa el id con `python3 -m knowledge.repos --vault-dir <vault> --json`. |
| `E_CONTEXTS_CONFLICT` | `doctor --client`: el paquete de un checkout no se puede instalar porque el checkout ya tiene ahí un archivo, directorio o enlace propio sin versionar (el mensaje nombra la aplicación, la ruta y la entrada). Lo que el checkout versiona en git nunca es conflicto. Corre `init --force` para apartarlo como `<nombre>.pre-ai-context.bak-<epoch>` (nada se borra) o quítalo a mano. |
| `E_CONTEXTS_CHECKOUT_INVALID` | `doctor --client`: un checkout no es el repositorio declarado (su `origin` es otro), no es un repositorio git o su `origin` no se puede leer (`unverified`), o su paquete no se puede instalar en él (un paquete sin `CLAUDE.md`, un checkout sin git y sin `--exclude gitignore`, un checkout dentro de un vault). Corrige la raíz o fija `repos.<id>` en el `repos.local.json`; un checkout que sabes que es el declarado se instala con `init --force`. |
| `W_CONTEXTS_CHECKOUT_UNVERIFIED` | `doctor --client`, aviso con código 0: un checkout es un repositorio git sin remoto `origin` (un clon que todavía no se publica), así que no se puede verificar ni descartar. `init` lo sigue reportando `unverified` y no instala nada en él sin `--force`. Dale su `origin` cuando el repositorio exista, o instálalo con `init --force` si sabes que es el declarado. |
| `W_CONTEXTS_LINKED_NOT_SELECTED` | `init` y `doctor --client`, aviso con código 0: un checkout tiene enlaces hacia su paquete (`Contexts/<paquete>/`) y no está en la lista `apply` del `repos.local.json` de la máquina, así que `init` ya no lo mantiene. Agrégalo con `init --repo ID` o quita sus enlaces con `init --drop ID`. |
| `W_CONTEXTS_SELECTION_MISSING` | `doctor --client`, aviso con código 0: el `repos.local.json` de la máquina no tiene la lista `apply` (lo escribió un `init` anterior a ella). El próximo `init` la crea con los checkouts que ya tienen su paquete instalado; para elegir otros, `init --repo ID` o `init --all`. |

Los avisos (`W_*`) no detienen el arranque, pero hay que leerlos: `W_REVIEW_THRESHOLDS_FALLBACK`, `W_VAULT_REFRESH_FAILED` (el entrypoint no pudo actualizar el clon de un vault y sirve lo que hay en disco), `W_WEBHOOK_SECRET_MISSING`, `W_VAULT_PATH_IGNORED`, `W_VAULT_SLUG_MISMATCH`, `W_FEATURE_WITHOUT_REPO`, `W_VAULT_GIT_PULL_OFF` (el vault está en un checkout de git pero no declara repositorio, así que reindexar no hace `git pull`: define `VAULT_REMOTE` o `repo`), `W_CONTRACT_RANGE`, `W_TRANSPORT_UNKNOWN`, `W_WIKILINK_AMBIGUOUS_STEM`, `W_TYPE_UNKNOWN` (`vault-lint` con las reglas 1.3: el `type` de una nota está fuera de `hub`, `service`, `architecture`, `flow`, `runbook`, `convention`, `decision`, `reference`, `note`, `pendientes` y `memory`), `W_SERVICES_MAP_MISSING` (`vault-lint` con las reglas 1.3: el vault tiene runbooks y ningún `services.json` contra el cual revisar su `service`), `W_EXTENSION_UNCONFIGURED` (por stdio, una extensión declarada sin sus credenciales: el servidor arranca y solo esa extensión responde con error) `W_EXTENSION_UNAVAILABLE_HTTP` (por HTTP, un vault declara `jira` o `memory-bridge`, que escriben en un memory local y no se cargan en un despliegue: sus tools no aparecen; también nombra una clave que solo esas extensiones leen, como `integrations.jira.kind_map` o `memory.hub`, con un valor que por stdio sería `E_MANIFEST_INVALID`), `W_BEARERS_IGNORED_STDIO` (`vaults.yaml` declara `bearers` y el servidor corre por stdio, donde no se comprueba ningún bearer), `W_RERANK_KEY_MISSING` (un vault declara un proveedor de `rerank` y la variable de su clave, que el aviso nombra, está vacía: el servidor arranca, también por HTTP, y ese vault busca sin juez hasta que la exportes) `W_RERANK_INVALID` (el bloque `rerank` de un `vault.yaml`, o una variable `KNOWLEDGE_RERANK*`, está mal formado: solo ese vault queda sin juez; el aviso nombra la clave a corregir) , `W_CONTEXTS_INIT_PENDING` (`doctor` por stdio, o `doctor --client` en cualquier transporte: un vault servido declara `Contexts/config.json` y esta máquina no tiene su `repos.local.json`, o con `--client` le queda algún checkout por instalar; los paquetes de contexto no están enlazados en los checkouts hasta que corras `cloudfrog-knowledge init` o el comando que nombre `KNOWLEDGE_COMMAND_LABEL`), `W_CONTEXTS_CHECKOUT_UNVERIFIED` (`doctor --client`: un checkout es un repositorio git sin remoto `origin`; código 0, ver la tabla de arriba), `W_CONTEXTS_LINKED_NOT_SELECTED` y `W_CONTEXTS_SELECTION_MISSING` (`init` y `doctor --client`: la selección `apply` de la máquina; código 0, ver la tabla de arriba) y `W_CONTEXTS_GITIGNORE` (`init`: el `.gitignore` de un vault que es un repositorio git no cubre `Contexts/*/.claude/settings.local.json`, `agent-memory/` o `worktrees/`, que Claude Code escribe en el vault a través de un paquete enlazado; no bloquea, y se corrige por PR del vault).
