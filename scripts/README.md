# Scripts

## setup.sh -- First-Time Setup

Installs dependencies, creates `.env` and runs `cloudfrog-knowledge migrate-index`, which prepares the vault's `index.sqlite` without embedding anything. The index rebuilds from the vault on the first tool call; there is no snapshot to download or upload.

```bash
./scripts/setup.sh
```

## weekly-stale-report.py — Reporte de documentos vencidos, por dueño

Lee el frontmatter del vault versionado, aplica los umbrales por tipo de
`review.thresholds_days` (el `vault.yaml` del vault, o el `config.json` que nombre
`KNOWLEDGE_CONFIG_JSON`) y emite la lista de documentos
vencidos **agrupada por el equipo que tiene que revalidarlos**.

No usa OpenAI y no necesita un índice: el mismo
comando corre en una laptop y dentro de un job de GitHub Actions con
nada más que el clon del repositorio.

### Cómo se resuelve el dueño

Solo los hubs `type: service` declaran `owner` (contrato del vault:
`Conventions/Frontmatter.md`). Los runbooks y playbooks declaran `service`.
El reporte recorre la cadena `documento.service → hub type:service →
hub.owner`, así que entrega el dueño sin duplicar el campo en cientos de
documentos curados a mano. Cuando la cadena no resuelve, el documento cae en
el grupo `unassigned` en vez de asignarse a alguien por conveniencia.

La misma resolución la usa la tool `get_stale_docs` (`knowledge/review.py`),
de modo que el reporte y la tool no pueden dar respuestas distintas.

### Uso

```bash
# Reporte completo en texto
uv run python scripts/weekly-stale-report.py

# Solo la cola de un equipo
uv run python scripts/weekly-stale-report.py --owner dev-php

# Artefactos para un build
uv run python scripts/weekly-stale-report.py --format markdown --output reports/stale-report.md
uv run python scripts/weekly-stale-report.py --format json    --output reports/stale-report.json

# Fecha de referencia fija (reproducible)
uv run python scripts/weekly-stale-report.py --today 2026-09-05

# Exigir cero vencidos (sale con código 1 si hay alguno)
uv run python scripts/weekly-stale-report.py --fail-on-stale
```

Códigos de salida:

| Código | Cuándo |
|---|---|
| `0` | Reporte emitido. Con `--fail-on-stale`: el vault quedó demostrado limpio. |
| `1` | Se pasó `--fail-on-stale` y hay algo que atender: un documento vencido, **o** uno cuya antigüedad no se puede leer. |
| `2` | No se pudo mirar: el vault no existe, `--today` no es una fecha válida, `--owner`/`--type` no coinciden con nada del vault, o el escaneo no leyó ningún documento curado. |

Dos decisiones deliberadas ahí:

- **`--owner` o `--type` que no existen se rechazan.** Un dueño mal escrito
  filtraba todo, dejaba `totals.stale` en cero y ponía el gate en verde
  encima de un vault con documentos vencidos. Un filtro que no coincide con
  nadie no demuestra que nadie esté vencido: demuestra que el filtro está
  mal. El mensaje enumera los valores que sí existen.
- **Un escaneo que leyó cero documentos no es un aprobado.** Es una ruta
  equivocada o un clon vacío. `OK: 0` nunca vale como PASS.

Quedan fuera del corpus, tanto aquí como en `get_stale_docs`: lo que cuelga
de un directorio con punto (`.trash`, `.space`, `.obsidian`), la carpeta de
memorias exportadas de memory, y las plantillas de Obsidian cuyo frontmatter
todavía tiene marcadores `{{date}}`.

### Lo que no se puede leer se lista, no se calla

Un documento puede escaparse de las reglas de revisión de dos maneras, y las
dos se reportan aparte con nombre y dueño:

- **Frontmatter ilegible**: ningún parser de YAML lo acepta, así que el
  índice tampoco lo ve.
- **`last_updated` ilegible**: el campo está pero no es una fecha
  (`not-a-date`, o el marcador `YYYY-MM-DD` de una plantilla sin renderizar).
  Este caso era invisible en las dos rutas: no salía vencido, no contaba como
  «sin `last_updated`» y no aparecía en ninguna lista. «No hay dato» y «no
  pude leer el dato» son respuestas distintas y ahora se cuentan distinto.

Ninguno de los dos se convierte en «vencido»: inventar una antigüedad sería
peor que no reportarla. Se listan para que alguien repare el frontmatter, y
cuentan para `--fail-on-stale`.

## notify-stale-report.sh — Aviso al dueño

Publica el reporte en un webhook de chat. La URL se lee de el secreto de
repositorio `STALE_REPORT_WEBHOOK_URL` (GitHub Actions secret) y nunca se imprime, ni siquiera cuando la petición falla. Los
diagnósticos de `curl` sí se muestran, pero pasados por un redactado que
sustituye la URL del webhook y cualquier otra URL absoluta que `curl`
mencione (un destino de redirección, por ejemplo).

Sin esa variable el script es un no-op documentado y sale con `0`, así que el
workflow queda verde hasta que alguien provisione el webhook.

```bash
STALE_REPORT_WEBHOOK_URL=... bash scripts/notify-stale-report.sh reports/stale-report.md
```

Códigos de salida:

| Código | Cuándo |
|---|---|
| `0` | Enviado, o no-op documentado (no hay webhook configurado). |
| `1` | Se intentó entregar y falló: respuesta que no es 2xx, o `curl` no completó la petición. |
| `2` | No existe el archivo de reporte que se le pidió enviar. |
| `3` | Falta un binario del que depende (`curl`, `python3`). No se pudo ni intentar. |

**Por qué el `3` existe.** Una imagen mínima como `python:3.12-slim`
trae `python3` pero **no** trae `curl`. El script mandaba la salida de error
de `curl` a `/dev/null`, así que `curl: command not found` llegaba al
operador como «el webhook no respondió (fallo de red o URL inválida)» — con
el receptor vivo dentro del mismo contenedor. Ahora las dependencias se
verifican antes que nada, incluso antes del no-op: un paso que no podría
enviar aunque estuviera configurado está roto **hoy**, y enterarse el día que
se cargue el secreto es exactamente el fallo que este guard evita. El job
que llama al aviso en el workflow del vault corre en `ubuntu-latest`, que ya
trae `curl` y `ca-certificates`.

Variables que lee (solo los nombres; los valores viven en la configuración del repositorio en GitHub):

| Variable | Obligatoria | Para qué |
|---|---|---|
| `STALE_REPORT_WEBHOOK_URL` | no (sin ella no notifica) | Webhook de chat que recibe el reporte. Va como *secret* del repositorio. |
| `STALE_REPORT_MAX_CHARS` | no (default 3500) | Corte del mensaje de chat. El reporte completo siempre queda como artefacto de la corrida. |

Batería de canarios (13 casos: los cuatro códigos de salida fijados con
igualdad exacta, las dos dependencias ausentes, y la no filtración de la URL
en cada rama — incluida la rama que sí imprime el diagnóstico de `curl`,
porque «no filtra» se satisface trivialmente no imprimiendo nada):

```bash
/bin/bash scripts/tests/notify-stale-report-canaries.sh
```

Conviene correrla también en Linux, no solo en una laptop: una batería que
únicamente se ejecutó en macOS no dice nada sobre la runner de Linux donde el
paso falló.

## Cadencia: launchd local → GitHub Actions

Un agente de `launchd` solo dispara mientras una laptop concreta está encendida y
con sesión iniciada. Sirve para lo que nace en esa laptop —el sync de Confluence y
la memoria local de memory—; no sirve para una cadencia de gobierno que varios
equipos van a mirar.

Por eso la revisión semanal del vault no se agrega como un agente local: vive en el
workflow `stale-report.yml` del repositorio del vault. Ese workflow trae
`scripts/weekly-stale-report.py` y `scripts/notify-stale-report.sh` de este
repositorio en un commit o tag fijado, genera los tres formatos como artefactos de
la corrida y llama al aviso. Su cadencia, sus secrets (`KNOWLEDGE_DEPLOY_KEY`, y
opcionalmente `STALE_REPORT_WEBHOOK_URL`) y sus variables (`STALE_REPORT_NOTIFY`,
`STALE_REPORT_MAX_CHARS`) son configuración de ese repositorio en GitHub, no un
archivo de este.

Los agentes de `launchd` siguen siendo los responsables del sync de Confluence y del
puente de memory, que no son parte de la revisión semanal. El índice del servidor no
tiene agente local: se reindexa cuando el webhook de push de GitHub le avisa (ver
«Sync del vault» en el README principal), y el puente de memory entra por esa misma
puerta porque termina con un push.

## weekly-memory-bridge.sh — Puente memory → vault (launchd semanal)

Un agente de `launchd` (por ejemplo, domingo 20:00 con su log en `/tmp`) corre este script, que encadena cuatro
pasos sobre la rama `main` del **repositorio del vault**: el checkout al que
apunta `VAULT_PATH` o, sin él, el vault por defecto de `vaults.yaml` (un clon del
repositorio del vault). Este repositorio no recibe
ningún commit del puente:

1. `git pull --ff-only`: la exportación es incremental sobre el estado
   commiteado (`90 - Memory/memory/.memory-sync-state.json`), y un push desde
   una rama atrasada se rechaza igual.
2. `export_memories.py --project knowledge`: vuelca en `90 - Memory/`
   las observaciones de tipo `decision` y `discovery` (config
   `memory.export_types`), quita hubs, tipos fuera de la allowlist y memorias
   ya promovidas (`knowledge_ref`).
3. `memory sync --cloud`: replica la memoria local al memory-cloud del stack.
   Si falla, avisa y sigue: no bloquea el vault.
4. `git commit` + `git push` de **solo** `90 - Memory/` en el repositorio del
   vault. El push dispara el webhook de reindex del servidor. Si lo único que
   cambió es la marca de tiempo del estado, no hay commit: ese push solo
   dispararía el reindex para nada.

Variables: `MEMORY_PROJECT` (default `knowledge`), `BRIDGE_BRANCH`
(default `main`; en cualquier otra rama sale con código `3` sin tocar nada),
`MEMORY_BIN`. Códigos de salida propios: `2` argumento desconocido, `3` rama
equivocada en el repositorio del vault, `4` no se pudo hacer fast-forward, `5`
el vault no es un checkout de git.

Ensayo sin commit ni push, con el mismo entorno mínimo que usa `launchd`:

```bash
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME=$HOME BRIDGE_DRY_RUN=1 \
  /bin/bash scripts/weekly-memory-bridge.sh
```

Exporta de verdad (los archivos quedan en el working tree), imprime el
`git status --porcelain` de `90 - Memory/` y hace `git push --dry-run` como
prueba de autenticación. Para forzar una corrida real:

```bash
launchctl kickstart gui/$(id -u)/<etiqueta del agente>
tail -f <log del agente>
```

## build-platform-graph.sh — Grafo de plataforma multi-repositorio

Fusiona los grafos por repositorio de `graphify` en un solo `merged-graph.json` de
plataforma y escribe, al lado, un sidecar de procedencia con el `built_at_commit` de
cada insumo. El sidecar existe porque `graphify merge-graphs` pierde ese dato: su
salida tiene cinco claves de primer nivel y ninguna es `built_at_commit`, y ADR-026
prohíbe una tarjeta de proyecto con resumen de grafo y `graph_commit` nulo.

Los insumos por omisión son los declarados en `graph.targets` del
`Contexts/config.json` del vault por defecto (ADR-039, ADR-047), resueltos por el registro de repositorios (`knowledge.graph_targets.graph_targets_detail()`,
invocado con `uv run` desde este propio checkout). Un objetivo declarado se fusiona
solo si resuelve a un directorio en esta máquina **y** ese directorio ya tiene
`graphify-out/graph.json` — el script nunca construye un grafo, eso es trabajo de
`bootstrap-graph`, repositorio por repositorio. Todo lo demás —sin resolver en esta
máquina, sin grafo todavía, u obsoleto frente al HEAD de su repositorio sin
`--allow-stale`— se omite y se nombra, nunca desaparece en silencio: queda en
`skipped_targets` del sidecar y en la tabla «Objetivos de `graph.targets` sin fusionar»
del reporte `.md`.

Ningún insumo va escrito a mano en el script: todos salen de `graph.targets` y del
registro de repositorios. Un checkout vendorizado dentro de otro se lee en su sitio,
como cualquier otro objetivo, siempre que tenga su propio `.git`.

```bash
# Corrida normal: artefactos en platform-graph/ (ignorado por git)
./scripts/build-platform-graph.sh

# Otro destino, o insumos explícitos
./scripts/build-platform-graph.sh --out /tmp/pg \
  --source /ruta/a/repo-a/graphify-out/graph.json \
  --source /ruta/a/repo-b/graphify-out/graph.json::/ruta/a/repo-b
```

Códigos de salida, cada uno con al menos un canario que lo produce y otro que no:

| Salida | Qué la dispara |
|---|---|
| 0 | grafo fusionado y sidecar escritos (puede traer objetivos declarados omitidos: eso queda registrado, no es un fallo) |
| 2 | error de uso, insumo inexistente, repositorio declarado que git no puede leer, dos insumos que resolverían al mismo prefijo de fusión, o menos de dos insumos sobreviven al descubrimiento/filtro de obsolescencia |
| 3 | insumo sin `built_at_commit` (ADR-026 no admite un hecho de grafo sin commit) |
| 4 | un `--source` explícito obsoleto frente al HEAD de su repositorio, degradable con `--allow-stale`; un objetivo de `graph.targets` obsoleto en cambio se omite (no muere) salvo que se pase `--allow-stale`, caso en que se incluye con `fresh: false` |
| 5 | el escaneo de credenciales no salió limpio: encontró una forma, o no pudo correr. En los dos casos borra el temporal y no publica |
| 6 | retirado. Ninguna ruta del script lo produce; queda fuera de reuso para que una referencia vieja a «salida 6» no se reinterprete como otra cosa |
| 7 | falló `graphify merge-graphs` |
| 8 | dos insumos vienen de dos checkouts distintos del mismo repositorio. El grafo lee cada repositorio por una sola copia: un repositorio puede estar varias veces en disco, un slot de despliegue cada una, y fusionar superficies de dos slots describe un árbol que no existe en ninguno. Dispara sobre el par de **checkouts**, no sobre el par de commits, porque dos slots pueden coincidir en `HEAD` hoy y divergir con el commit siguiente (ADR-048 §3) |

Ningún gate falla en abierto: «no encontré nada» y «no pude mirar» son resultados
distintos y el segundo nunca deja publicar. Un repositorio declarado que git no puede
leer corta la corrida (salida 2) en vez de registrar `fresh: null`; un insumo sin
repositorio *ninguno* sí sigue, pero avisa y queda en el sidecar como `fresh: null`.

La identidad que compara la salida 8 es la URL del remoto `origin`, normalizada sin
barra final ni sufijo `.git`. Un insumo sin `origin` es «identidad desconocida», nunca
«identidad coincidente»: cae a su propia raíz de checkout, que solo puede chocar
consigo misma, y el script lo avisa. Cada insumo del sidecar lleva además `repo_root`,
`repo_identity` y `repo_branch` —la rama, porque el commit solo no distingue dos copias
en ramas distintas mientras ninguna haya avanzado—, y el sidecar cierra con
`sibling_checkouts`: las demás copias de trabajo de cada repositorio fusionado que el
registro conoce, con su rama y sus archivos sin commitear. No son insumos y no gatean
nada; están para que lo que el artefacto deja fuera quede escrito. Una corrida con
`--source` explícito no consulta el registro y las deja vacías.

El escaneo de credenciales vive en `scripts/lib/scan-credential-shapes.py` y se puede
correr solo sobre cualquier archivo (0 limpio, 1 encontró, 2 no pudo mirar). Informa la
regla y el número de línea, nunca el valor. Tiene tres capas, porque anclarse a
prefijos de proveedor solo encuentra credenciales de proveedores que publican prefijo:

1. **Prefijos** conocidos: `AKIA`, `ghp_`, `AIza`, `ATATT`, `sk-ant`, `eyJ...`, PEM.
2. **Forma en contexto**: `<nombre> = <valor>` y `?<nombre>=<valor>` **con las comillas
   opcionales**, más los nombres de parámetro de autenticación heredados (`authpt`, `authpw`,
   `authtoken`), que no llevan prefijo ni palabra clave. Exigir comillas es lo
   que dejaba pasar `password=<valor>`; anclar a prefijo es lo que dejaba pasar
   `authpt=<valor>`.
3. **Valor opaco sin contexto**: una cadena alfanumérica de 32 o más caracteres que
   mezcla minúsculas, mayúsculas y dígitos y además cambia de clase de carácter en al
   menos el 45 % de sus pares contiguos o carga 4.5 bits de entropía de Shannon por
   carácter.

La capa 3 es una heurística y se documenta como tal. Medida contra 20,000 tokens
aleatorios por longitud detecta el 98.8 % a 32 caracteres, el 99.9 % a 40 y el 100 % a
64. Medida contra el grafo de plataforma publicado (138.7 MB, 3,677,412 líneas) da cero
falsos positivos: los identificadores tipo `isOutgoingServer2serverGroupShareEnabled`
se quedan en 0.31 de cambio de clase y 4.10 bits, y los digests hexadecimales en
minúsculas nunca califican porque no traen mayúsculas.

Límite medido de la herramienta: `merge-graphs` no infiere ni una arista entre
repositorios, así que el grafo de plataforma es un conjunto de componentes
desconectados y `graphify path` entre dos servicios no responde. Sirve para inventario
y métricas; la navegación simbólica sigue siendo por repositorio. El detalle está en
`platform-graph/merged-graph.report.md` y en ADR-023.

Batería de canarios. Corre en el `bash` 3.2 que trae macOS y en `bash` 5; fija cada
código de salida por igualdad exacta, nunca por «distinto de cero»:

```bash
/bin/bash scripts/tests/build-platform-graph-canaries.sh
/opt/homebrew/bin/bash scripts/tests/build-platform-graph-canaries.sh
```

El grupo C —el conjunto de insumos por omisión— declara su propio registro y sus
propios `graph.targets` en un `config.json` sintético bajo `tmp`. El seam es `PYTHONPATH`: `knowledge.config` deriva la
raíz del proyecto de la ubicación resuelta del propio paquete, así que una copia de
`src/` bajo la raíz sintética convierte a esa raíz en la raíz del proyecto. Un symlink
no sirve — `resolve()` lo sigue de vuelta al checkout real. El caso C0 comprueba el
seam antes de medir nada a través de él, para que un seam roto se reporte como tal y
no como cuatro regresiones del gate. Así la batería no depende de lo que declare la
máquina donde corre.

## promote-memories.py — Promoción de una memoria a conocimiento curado

El puente inverso de `export_memories.py`. Aquel vuelca memorias en `90 - Memory/`
como notas de segunda, aisladas de la búsqueda; este toma una observación que se lo
ganó —pinneada y de un tipo de la allowlist, `decision` o `discovery`— y la convierte
en un documento con dueño.

**El vault es de solo lectura para este puente.** Su único escritor es un pull request
fusionado. El script genera los documentos candidatos fuera del vault, junto con un
patch que los agrega, y ahí se detiene. Si el directorio de salida resolviera dentro
del vault, aborta antes de llamar a memory.

El patch es relativo a la raíz del repositorio que contiene el vault (por ejemplo,
un clon del repositorio del vault, cuya raíz es el vault: las rutas empiezan en
`Architecture/…`). Se aplica en ese checkout, nunca en este repositorio. Un vault que no
está dentro de un repositorio git sale con código `2`.

### Los dos pasos, en el orden en que van

```bash
# 1. Antes del PR: arma el paquete. No toca ni el vault ni memory.
uv run python scripts/promote-memories.py plan --project nextcloud

# 2. Después de fusionar el PR: marca knowledge_ref en cada observación.
uv run python scripts/promote-memories.py stamp \
  --manifest build/promotions/<corrida>/manifest.json
```

`stamp` comprueba que el documento exista de verdad en el vault antes de marcar nada.
Marcar antes de la fusión dejaría la observación apuntando a un documento que ningún
checkout tiene, que es justo lo que `memory doctor --check knowledge_ref_dangling`
reporta como defecto.

### Qué produce `plan`

En `build/promotions/<corrida>/` (configurable en `config.json`,
`memory.promotion.output_dir`):

| Archivo | Qué es |
|---|---|
| `documents/<ruta en el vault>` | Un documento por candidata, con el contenido de la observación tal cual y la procedencia en el frontmatter |
| `promotion.patch` | Patch de creación, con el blob id real de git en la línea `index`, listo para `git apply` |
| `manifest.json` | El registro de la corrida: contadores del escaneo, y por candidata su `sync_id`, su ruta, su `knowledge_ref` y su sha256 |
| `PR.md` | La descripción del pull request, con lo inspeccionado, los documentos y el comando de `stamp` |

### La allowlist vive en memory, no aquí

`decision` y `discovery` comparten una propiedad que nada más tiene: enuncian un hecho
que sobrevive a la sesión que lo produjo. Lo demás que memory guarda —`bugfix`,
`manual`, resúmenes de sesión— es rastro de un trabajo, y un vault lleno de rastros es
un vault que nadie lee.

La lista está en el código de memory (`internal/store/projects_promotion.go`) y no en
configuración a propósito: ampliarla es una decisión de gobernanza con revisión, no una
bandera que alguien mueve de madrugada. `--types` solo puede **restringir**; nombrar un
tipo fuera de la allowlist es un error, nunca una ampliación.

### Carpeta destino por tipo

`config.json`, `memory.promotion.target_folders`. Un tipo sin carpeta declarada es un
error y no un valor por omisión: dejar caer un documento promovido en una carpeta
verosímil es como un vault termina con una taxonomía que nadie acordó.

`decision` y `discovery` entran como tipos nuevos de documento del vault. La
alternativa era reetiquetarlos como `architecture` o `reference`, que afirmaría que una
persona los escribió así. Al no estar en `review.thresholds_days`, caen en el umbral
`default` (270 días), que es la cadencia correcta para un hecho que nadie ha
revalidado todavía.

### Códigos de salida

| Código | Significado |
|---|---|
| 0 | La corrida terminó y su salida es confiable |
| 2 | Nada utilizable que inspeccionar: cero observaciones pinneadas, salida dentro del vault, vault ilegible, manifiesto vacío, o un filtro que no nombra nada promovible |
| 3 | La corrida es parcial y hace falta una persona: una candidata choca con un documento que ya existe (`plan`), o un documento sigue sin estar en el vault porque el PR no se ha fusionado (`stamp`) |
| 4 | El entorno no pudo correr el puente: falta una dependencia, falta el binario de memory, o una llamada a memory falló |

Cero unidades inspeccionadas nunca es un PASS: un proyecto sin nada pinneado sale con
código 2 y lo dice, en vez de reportar «0 candidatas» como si hubiera mirado.

### Batería de canarios

```bash
uv run pytest tests/test_promotion_bridge.py -q
```

## bump-version.sh / check-version.sh -- Versión del paquete y del plugin

`bump-version.sh X.Y.Z` escribe el mismo número en `version` de `pyproject.toml`, en `version` de `plugin/claude-code/.claude-plugin/plugin.json` y en el `@vX.Y.Z` de la fuente del paquete en `plugin/claude-code/.mcp.json`, regenera `uv.lock` con `uv lock` (`--no-lock` lo omite) y termina con `check-version.sh`. No hace commit ni corta el tag: el tag se crea sobre el merge del pull request de la release.

`check-version.sh [vX.Y.Z]` compara esos tres valores y la versión del proyecto en `uv.lock`; también el tag, si se pasa como argumento o si `HEAD` tiene uno `v*`. Solo lectura. Sale con 0 si todo coincide, 1 si algo difiere y 2 con un error de uso.

```bash
scripts/bump-version.sh 0.2.0
scripts/check-version.sh v0.2.0
```

`.github/workflows/release-check.yml` lo corre en cada tag `v*`: con el nombre del tag cuando el ref es un tag y sin argumento cuando se despacha desde una rama, porque el nombre de una rama no es un tag y el script lo rechaza con 2.
