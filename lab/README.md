# Lab dockerizado

Prueba, con tenants falsos, que el servidor no está casado con ninguna organización: **un solo compose** (`infra/compose.yml`), **N instancias**, y a cada instancia se le inyectan sus vaults por entorno. Además prueba el otro lado: **dos clientes falsos** que corren `cloudfrog-knowledge init` en el host, como una persona que tiene su vault clonado y habla con un servidor remoto.

```
acme    -> knowledge-acme    127.0.0.1:18101   acme-handbook (git, file://)   acme-ops (carpeta)
globex  -> knowledge-globex  127.0.0.1:18102   globex-docs   (git, file://)   globex-notes (carpeta, sin apps)

alice (acme)   HOME=lab/.work/clients/alice   acme-web y acme-deploy en Projects/; acme-api no está
bob   (globex) HOME=lab/.work/clients/bob     globex-core en Projects/; globex-notes no declara apps
```

Cada instancia tiene su bearer de lectura, su bearer `admin`, su índice, sus clones y su red. Los embeddings son `bag-of-words`: sin clave de OpenAI y sin red (`OPENAI_API_KEY` va vacía a propósito).

## Uso

Requiere `docker` (compose v2), `git`, `curl` y `uv`.

```bash
lab/run.sh          # siembra, construye, levanta, espera /health y corre las dos tablas de comprobaciones
lab/run.sh --check  # solo las comprobaciones, contra las instancias que ya corren
lab/run.sh --down   # borra contenedores, volúmenes, redes y lab/.work
```

`run.sh` termina con 0 solo si todas las filas de ambas tablas son PASS.

## Qué hay

- `seed.sh` arma `lab/.work/` (ignorado por git). Servidores: dos repos bare (`repos/acme-handbook.git`, `repos/globex-docs.git`, que el contenedor ve por `file:///repos/...`) y las carpetas `vaults/acme-ops` y `vaults/globex-notes`. Cada vault trae `vault.yaml` (con `index.exclude: ["Contexts/**"]`), un `.gitignore` con `Contexts/*/.claude/settings.local.json`, tres notas que pasan `vault-lint` 1.3 (con su `services.json`), `90 - Memory/` y `Contexts/`: un `config.json` con la sección `repos` y, por app, `CLAUDE.md`, `.claude/agents/x.md` y `.mcp.json`. `acme-handbook` declara además `features: [governance]` (para `get_stale_docs`), una nota vencida (`Architecture/Legacy Notes.md`) y un `history.md` viejo dentro de `Contexts/acme-web/`. `globex-notes` es el vault sin apps: su `config.json` trae `roots: []`, `entries: []` y `graph.targets: []`. `seed.sh --clients` rehace solo los clientes.
- Clientes: cada uno tiene su `HOME`, su `XDG_CONFIG_HOME` (`.config/knowledge/vaults.yaml`), su copia local de cada vault (un clon del repo bare, o la carpeta misma para `acme-ops`) y los checkouts falsos (`git init` + `origin`) bajo `Projects/`. Alice tiene además un árbol sintético de ~2.000 directorios con 60 repos que no coinciden con nada, para medir el escaneo.
- Cada tenant tiene palabras que los otros nunca usan: `zephyr` (acme), `quokka` (globex), `marmot` (acme-handbook), `nautilus` (acme-ops), `pangolin` (globex-docs) y `lemur` (globex-notes). Cada paquete de contexto tiene una palabra en su `CLAUDE.md` y otra en su `.claude/agents/x.md`.
- `compose.yml` extiende el servicio `knowledge-mcp` de `infra/compose.yml` dos veces; lo único propio de cada instancia es `KNOWLEDGE_VAULTS_YAML`, sus bearers, su puerto y sus volúmenes. Los bearers se generan en `lab/.work/lab.env` (permisos 0600), nunca se versionan.
- `check.py` habla con ambos servidores por HTTP con el SDK de `mcp`. `check_clients.py` corre `uv run --no-sync cloudfrog-knowledge ...` como cada cliente, con un entorno mínimo (solo `PATH`, su `HOME`, su `XDG_CONFIG_HOME` y la caché de uv): nada del entorno ni del home reales llega al proceso. Empieza rehaciendo los clientes, así que se puede repetir con `--check`. `report.py` imprime la tabla.

## Qué comprueba

Servidores (`check.py`):

| Comprobación | Qué demuestra |
|---|---|
| `list_vaults` | acme lista sus dos vaults y globex los suyos (`globex-docs` por defecto y `globex-notes`) |
| vault sin apps (`globex-notes`) | se carga, se busca y se lee como cualquier otro; su palabra no aparece en `globex-docs` |
| `search` de la palabra propia | cada tenant encuentra lo suyo |
| `search` de la palabra ajena (modo `keyword`) | ningún tenant encuentra lo del otro |
| vault carpeta (`acme-ops`) | se busca y se lee igual que uno con git |
| `vault=` | los vaults de un mismo tenant no se mezclan; el vault de otro tenant no existe |
| sin bearer, bearer falso, bearer del otro tenant | 401 |
| `reindex` con el bearer de lectura | `E_SCOPE_REQUIRED`; con el `admin`, funciona |
| `POST /webhook/reindex` | 401 sin credencial, 403 con lectura, 202 con `admin` |
| `POST /webhook/reindex` firmado, a otra rama o borrándola | 200 con `"status": "ignored"` (`not_served_branch`, `ref_deleted`) y sin pull; el push a `main`, 202 |
| commit nuevo en el remoto + `reindex(pull=true)` | el vault con git sigue a su remoto |
| palabra de `Contexts/<app>/CLAUDE.md` y de `.claude/agents/x.md` | el `CLAUDE.md` se indexa; lo que cuelga de `.claude/` no |
| `get_stale_docs(vault="acme-handbook", missing_last_updated=true)` | lista la nota vencida del vault y nada bajo `Contexts/` (ni el `history.md` viejo del paquete ni los `CLAUDE.md` sin fecha): `index.exclude` los saca de la cola |

Clientes (`check_clients.py`, las mismas filas para alice y para bob):

| Comprobación | Qué demuestra |
|---|---|
| `doctor --brief` antes de `init` | dice `init pending` y nombra solo los vaults que tienen apps que enlazar |
| `init --dry-run` | planea `would-install` y no escribe nada (ni mtimes) |
| `init` | sale 0, instala las apps presentes y reporta `skipped` la ausente |
| `<xdg>/knowledge/<slug>/repos.local.json` | existe para cada vault con apps, con la raíz detectada por el `origin` |
| vault sin apps (bob) | `init` lo reporta `no-apps` y sale 0, no escribe su `repos.local.json` y `doctor` no lo cuenta como `init pending` |
| `CLAUDE.md`, `.mcp.json`, `.claude` | enlaces que resuelven a la copia local del vault del cliente |
| app ausente | `skipped` y no se crea ni su directorio |
| `git status` del checkout | limpio: los enlaces quedan en `.git/info/exclude` |
| árbol del vault antes y después | idéntico: `init` no escribe dentro del vault |
| `vault-lint` sobre cada copia del vault | sale 0: `Contexts/` queda fuera de las reglas de frontmatter por `index.exclude` |
| segunda corrida | todo `unchanged` y ningún archivo cambia (mtimes y hashes) |
| `.gitignore` de la copia del vault | `init` reporta `covered` para los clones (cubren `settings.local.json`, `agent-memory/` y `worktrees/`) y `not-applicable` para la carpeta sin git, sin avisos |
| `doctor --brief` después de `init` | `ok`: el hook de sesión calla |
| escaneo de ~2.000 directorios (alice) | encuentra los checkouts y reporta cuánto tarda |

## Contrato de inyección

El detalle vive en `docs/runbooks/servir-varios-vaults.md` (sección «Compose genérico»). En corto:

- `KNOWLEDGE_VAULTS_YAML`: el contenido de un `vaults.yaml`. Una entrada con `repo` se clona o actualiza en `/data/vaults/<slug>`; una con solo `path` es una carpeta ya montada y git no corre para ella.
- Llave de un remoto por ssh: `<SLUG_EN_MAYÚSCULAS>_GIT_DEPLOY_KEY_B64`, y si no existe, `GIT_DEPLOY_KEY_B64`. Los remotos `file://` y `https://` no la necesitan (el lab usa `file://`).
- Sin `KNOWLEDGE_VAULTS_YAML`, el servidor sirve un vault con `VAULT_PATH`/`VAULT_REMOTE`/`VAULT_BRANCH`, como siempre.

## Notas

- `/webhook/reindex` y `/health` sirven el vault por defecto de cada instancia; los otros vaults con `repo` se actualizan con `reindex(vault=..., pull=true)`.
- Si cambias el `Dockerfile` o `infra/`, `run.sh` reconstruye la imagen. Para dejar el lab sin imágenes: `docker image rm knowledge-lab-knowledge-acme knowledge-lab-knowledge-globex`.
- El nombre de proyecto de compose es fijo (`knowledge-lab`): un `run.sh` nuevo reemplaza las instancias que haya levantado otro checkout.
- El escaneo de `init` lee el `origin` del archivo de config de git (sin lanzar `git` por repo; `include`, comillas, comentarios y demás casos raros sí van a `git`) y se comparte entre los vaults de una misma corrida. Con los 2.016 directorios y 62 repos de alice: ~60 ms de escaneo y ~0,45 s de `init`; lanzar `git` por repo costaría ~910 ms y ~2,3 s. `check_clients.py` imprime la medición de cada corrida.
- El lab no toca `infra/dokploy-compose.yml` ni ningún vault real.
