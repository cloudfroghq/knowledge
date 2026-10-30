# Operar el anillo 1 en GitHub Actions

Runbook para quien administra `cloudfroghq/knowledge`: qué corre en Actions, qué exige el ruleset `protect-main`, cuánto cuesta en minutos, cómo rotar una deploy key de lectura, cómo volver atrás sin dejar PRs bloqueados y cómo se publica una release en PyPI y npm. El reporte semanal de documentos vencidos no corre aquí: lo ejecuta el workflow de cada vault, que trae `scripts/weekly-stale-report.py` y `scripts/notify-stale-report.sh` de este repositorio. Qué hace cada job y por qué está en `README.md`, sección «Tests y compuertas de merge».

## 1. Estado

| Qué | Valor | Cómo comprobarlo |
|---|---|---|
| Actions | encendido; solo acciones de GitHub (`allowed_actions: selected`, `github_owned_allowed: true`, sin `verified_allowed` ni patrones); `sha_pinning_required: true` | `gh api repos/cloudfroghq/knowledge/actions/permissions` y `.../actions/permissions/selected-actions` |
| Token de los workflows | `default_workflow_permissions: read`; cada workflow declara `permissions: contents: read` | `gh api repos/cloudfroghq/knowledge/actions/permissions/workflow` |
| `ring1.yml` | `pull_request` a `main`, `push` a `main` y `workflow_dispatch`; ningún secret | `gh workflow view ring1.yml -R cloudfroghq/knowledge` |
| `release-check.yml` | push de tags `v*` y `workflow_dispatch`; ningún secret ni environment | `gh workflow view release-check.yml -R cloudfroghq/knowledge` |
| `publish.yml` | push de tags `v*` y `workflow_dispatch` (`dry_run`, verdadero por omisión); job `build` de solo lectura y job `publish` con el environment `release` e `id-token: write` (trusted publishing en PyPI y npm, sin secrets) | `gh workflow view publish.yml -R cloudfroghq/knowledge` |
| Secrets de repositorio | ninguno | `gh api repos/cloudfroghq/knowledge/actions/secrets --jq .total_count` → `0` |
| Environment `release` | política de tags `v*`, sin secrets; lo declara solo el job `publish` de `publish.yml`, y es el environment que nombran los trusted publishers de PyPI y npm | `gh api repos/cloudfroghq/knowledge/environments/release/deployment-branch-policies` |
| Trusted publishers | PyPI `cloudfrog-knowledge` y npm `@cloudfrog/knowledge`: dueño `cloudfroghq`, repositorio `knowledge`, workflow `publish.yml`, environment `release` | sección 7 |
| Deploy keys de lectura | una por vault que trae herramientas de este repositorio (`vault-lint`, reporte semanal), de solo lectura, registradas aquí | `gh repo deploy-key list -R cloudfroghq/knowledge` |

## 2. Checks obligatorios en `protect-main`

El ruleset `protect-main` (rama por omisión; su id sale de `gh api repos/cloudfroghq/knowledge/rulesets`) exige, además del PR, estos cinco contextos de la app GitHub Actions (`integration_id` `15368`), con `strict_required_status_checks_policy: false`:

- `Lint + pytest with coverage`
- `Layered architecture: import-linter (ADR-022)`
- `Secrets: gitleaks`
- `SAST: Semgrep CE`
- `Diff coverage (threshold from pyproject.toml)`

Son los jobs que corren en un `pull_request`. `Image: build and smoke test (amd64)` no es obligatorio: no corre en PRs para ahorrar minutos, y se valida en cada `push` a `main` y en cualquier `gh workflow run ring1.yml --ref <rama>`. Antes de mergear un cambio a `Dockerfile`, `infra/` o `tests/test_image.py`, despacha ring1 sobre la rama y espera la imagen en verde.

El nombre del job es el contexto del check: renombrar un job en `ring1.yml` sin cambiar el ruleset deja todo PR esperando un check que nunca llega. `tests/test_pipeline_contract.py` fija que los jobs de un PR sean exactamente esos cinco.

```bash
ID=$(gh api repos/cloudfroghq/knowledge/rulesets --jq '.[]|select(.name=="protect-main").id')
gh api "repos/cloudfroghq/knowledge/rulesets/$ID" \
  --jq '.rules[]|select(.type=="required_status_checks")|.parameters.required_status_checks[].context'
```

Un PR abierto antes de que existieran los checks no tiene ninguno y queda esperándolos. Se destraba con cualquier push a su rama (el evento `synchronize` dispara ring1) o con `gh pr update-branch <n> -R cloudfroghq/knowledge` cuando no tiene conflictos.

### Bypass

El rol de administrador (`RepositoryRole` 5) tiene bypass en modo `pull_request`: puede mergear un PR con checks pendientes o en rojo (`gh pr merge <n> --admin`), y el push directo a `main` sigue bloqueado. Ese bypass se usa solo ante una caída de GitHub o del runner, y el motivo se anota en el PR. Un check rojo por el código se arregla en el PR.

## 3. Minutos de Actions

En un repositorio privado cada job consume minutos Linux del plan de la cuenta dueña, redondeados al minuto por job; en uno público los runners estándar de GitHub no consumen minutos. Medido en corridas reales:

| Corrida | Jobs | Minutos facturables |
|---|---|---|
| ring1 en `pull_request` | 5 (la imagen se salta) | ~7 |
| ring1 en `push` a `main` o por `workflow_dispatch` | 5 (la cobertura del parche se salta) | ~7 |
| release-check (tag `v*` o despacho) | 1 | 1 (el job dura 36 s; corrida 38362509479) |
| `vault-lint` en un vault (PR, push a `main` o despacho) | 1 | ~1 (el job dura unos 20 s) |

Total medido al poner todo en marcha: unos 68 minutos facturables en 20 corridas el 2026-10-13 (ring1 por PR ≈7 min sin imagen, por push a `main` ≈7 min con imagen, `vault-lint` ≈1 min por corrida); medido cuando el repositorio era privado.

```bash
gh api organizations/cloudfroghq/settings/billing/usage/summary
gh run view <id> -R cloudfroghq/knowledge --json jobs --jq '.jobs[]|[.name,.startedAt,.completedAt]|@tsv'
```

`actions/runs/<id>/timing` responde `total_ms` en `0` para la cuenta y el reporte de uso por repositorio llega con atraso: el cálculo por job es la medida inmediata. Si los minutos del mes se agotan y no hay presupuesto de gasto, los jobs no arrancan y los cinco checks obligatorios bloquean todo PR: en ese caso se quita la regla en knowledge y en los vaults (sección 5, paso 1) y se vuelve a poner al cambiar el mes.

## 4. Rotar una deploy key de lectura

Los workflows de los vaults (`vault-lint`, reporte semanal) traen las herramientas de este repositorio con una deploy key de solo lectura registrada aquí; su mitad privada vive en el secret `KNOWLEDGE_DEPLOY_KEY` del vault y en un archivo con permisos `600` en la máquina del dueño. Nunca se imprime ni se copia a un documento.

```bash
F=<archivo nuevo, fuera de cualquier repositorio>
V=<owner>/<vault>
ssh-keygen -q -t ed25519 -N '' -C "ci knowledge $V (read-only)" -f "$F"
chmod 600 "$F"
gh repo deploy-key add "$F.pub" -R cloudfroghq/knowledge --title "<título de la clave>"   # solo lectura por omisión
gh secret set KNOWLEDGE_DEPLOY_KEY -R "$V" < "$F"
gh workflow run <workflow>.yml -R "$V" --ref main                                              # debe quedar en verde
gh repo deploy-key list -R cloudfroghq/knowledge --json id,title,createdAt
gh repo deploy-key delete <id de la clave vieja> -R cloudfroghq/knowledge
```

El aviso del reporte semanal a un chat (`STALE_REPORT_WEBHOOK_URL`, `vars.STALE_REPORT_NOTIFY`) se configura en el vault que corre el reporte, no aquí.

## 5. Rollback

El orden importa: si se apaga Actions con checks obligatorios activos, ningún PR vuelve a quedar en verde y todo merge necesita el bypass. Esto vale para knowledge y para cada vault que exige el check `vault-lint` en su propio `protect-main`. El id de `protect-main` de este repositorio y el de cada vault salen de `gh api repos/<owner>/<repo>/rulesets`.

Antes de ejecutar nada, confirma que cada id pertenece a su repositorio (la salida trae el repositorio, el id y el nombre):

```bash
for r in cloudfroghq/knowledge <owner>/<vault> ...; do
  gh api "repos/$r/rulesets" --jq '.[]|"\(.source) \(.id) \(.name)"'
done
```

```bash
K=cloudfroghq/knowledge
# 1) Quitar required_status_checks de protect-main en knowledge y en cada vault que lo exige.
drop_checks() {   # <repo> <id del ruleset protect-main>
  gh api "repos/$1/rulesets/$2" \
    | jq '{name,target,enforcement,conditions,bypass_actors,rules:[.rules[]|select(.type!="required_status_checks")]}' \
    | gh api -X PUT "repos/$1/rulesets/$2" --input -
}
drop_checks "$K" <id del ruleset protect-main de knowledge>
drop_checks <owner>/<vault> <id del ruleset protect-main de ese vault>   # una línea por vault
# 2) Apagar Actions en knowledge y el workflow de vault-lint en cada vault.
gh api -X PUT repos/$K/actions/permissions -F enabled=false
gh workflow disable vault-lint.yml -R <owner>/<vault>                    # uno por vault
# 3) Revocar las credenciales: las deploy keys de lectura registradas en knowledge y el secret de cada vault.
gh repo deploy-key list -R $K --json id,title
gh repo deploy-key delete <id de cada clave de vault> -R $K
gh secret delete KNOWLEDGE_DEPLOY_KEY -R <owner>/<vault>                  # uno por vault
```

Las deploy keys de los workflows de los vaults viven en knowledge (de solo lectura, una por vault); las que sirven a un despliegue se documentan en el vault de ese despliegue, en `Deploy/`, y el rollback no las toca.

La función recibe cada repositorio como argumento para funcionar igual en bash y en zsh (zsh no parte una variable sin comillas por espacios). Los cambios de YAML se revierten por PR (`git revert` del merge correspondiente). El despliegue automático de la plataforma no depende de Actions: el rollback no lo toca.

## 6. Verificar una release

`release-check.yml` es la verificación entre un tag y el pull request del marketplace. Corre en cada `git push origin v<x.y.z>` y a mano. Orden de una release:

1. El pull request de la release (`scripts/bump-version.sh X.Y.Z`) pasa ring1 y se mergea.
2. Antes de empujar el tag, ensayo sobre `main` con el mismo archivo que usará el tag: `gh workflow run release-check.yml -R cloudfroghq/knowledge --ref main` y `gh workflow run publish.yml -R cloudfroghq/knowledge --ref main` (construye y verifica sin publicar), y `gh run watch` de cada una. Sin tag, `check-version.sh` compara solo los archivos.
3. `git push origin v<x.y.z>` sobre el merge (tag firmado, sección 7). La corrida del tag compara además el tag con las versiones, y `publish.yml` publica en PyPI y npm.
4. Solo con `release-check` en verde se abre el pull request de `cloudfroghq/claude-plugins` que mueve el marketplace al tag nuevo.

```bash
RUN=$(gh run list -R cloudfroghq/knowledge -w release-check -e push -L1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN" -R cloudfroghq/knowledge --exit-status
```

Un despacho con `--ref v0.6.0` no sirve: usa el árbol del tag, que no contiene `release-check.yml`. Si falla `check-version.sh`, el tag no coincide con alguna de las cinco versiones (`pyproject.toml`, `plugin.json`, `.mcp.json`, `npm/package.json` y `uv.lock`): `publish.yml` falla en el mismo paso y no publica nada. Se corrige en un pull request y se corta un tag nuevo: un tag que ya publicó algo no se mueve, porque PyPI y npm no aceptan volver a publicar una versión. Si falla `claude plugin validate`, lo reproduces en local con `claude plugin validate --strict plugin/claude-code`.

## 7. Publicar en PyPI y npm

Cada tag `v<x.y.z>` sale por tres canales con la misma versión: el tag mismo (`uvx --from git+https://github.com/cloudfroghq/knowledge.git@v<x.y.z> …`), PyPI (`cloudfrog-knowledge`) y npm (`@cloudfrog/knowledge`, que corre la versión de PyPI con un `uv` fijo). `publish.yml` hace los dos últimos.

### Qué hace `publish.yml`

| Job | Permisos | Qué hace |
|---|---|---|
| `Build and verify the release` | `contents: read` | Instala `uv` desde su release, verificado contra `npm/lib/uv-release.json` (`scripts/ci-install-uv.sh`); compara el tag con las cinco versiones (`check-version.sh`); comprueba las sumas de `uv` del paquete de npm contra los `.sha256` de la release y lo instala con el código del paquete (`npm/scripts/uv-release.js --check` y `--smoke`); corre `npm test` y `npm pack --dry-run`; construye sdist y wheel; instala el wheel en un entorno limpio de Python 3.12 y exige `cloudfrog-knowledge <versión>` y `knowledge <versión>` de `--version`; sube `dist/`. |
| `Publish to PyPI and npm` | `contents: read`, `id-token: write`; environment `release` | Solo con un ref de tag: en el push del tag, o a mano desde el tag con `dry_run` desmarcado. PyPI primero (`uv publish --trusted-publishing always` con el wheel y el sdist del job anterior), después npm (`npm publish --provenance --access public` desde `npm/`, con npm 11.20.0). Antes de cada uno consulta el registro: `200` (esa versión ya existe) salta con un aviso, `404` publica y cualquier otra respuesta falla. |

`uv` ignora el tope de `requires-python`, así que el job instala el wheel con `--python 3.12` explícito, igual que el paquete de npm y el plugin.

### Una sola vez

1. **PyPI**: el publicador pendiente ya existe (proyecto `cloudfrog-knowledge`, dueño `cloudfroghq`, repositorio `knowledge`, workflow `publish.yml`, environment `release`). La primera corrida de `publish.yml` crea el proyecto y el publicador pasa a ser normal; se revisa en `https://pypi.org/manage/project/cloudfrog-knowledge/settings/publishing/`.
2. **npm, primera versión a mano**: npm solo deja configurar un trusted publisher sobre un paquete que ya existe, así que la primera versión la publica el dueño del scope `@cloudfrog` desde su máquina, con `main` en el merge de la release y antes de empujar el tag:

   ```bash
   cd ~/Projects/Tools/cloudfroghq-knowledge
   git switch main && git pull --ff-only
   scripts/check-version.sh                      # las cinco versiones en <x.y.z>
   cd npm
   npm login                                     # la cuenta dueña del scope @cloudfrog
   npm whoami
   npm test
   npm pack --dry-run                            # LICENSE, README.md, package.json, bin/ y lib/
   npm publish --access public                   # sin --provenance: la procedencia solo la firma CI
   npm view @cloudfrog/knowledge@<x.y.z> version
   ```

   Con 2FA en la cuenta, `npm publish` pide el código (o `--otp=<código>`). Esa versión queda sin procedencia; desde la siguiente la publica `publish.yml` con procedencia. Hasta que el tag publique en PyPI, `npx @cloudfrog/knowledge@<x.y.z>` no encuentra el paquete de Python: empuja el tag enseguida.
3. **npm, trusted publisher**: en `https://www.npmjs.com/package/@cloudfrog/knowledge/access` (Settings), sección «Trusted Publisher», elige GitHub Actions y llena: organización o usuario `cloudfroghq`, repositorio `knowledge`, archivo del workflow `publish.yml`, environment `release`. Guarda. Cuando una versión publicada por CI ya tenga procedencia (`npm view @cloudfrog/knowledge@<versión> dist.attestations`), en «Publishing access» elige «Require two-factor authentication and disallow tokens»: desde ahí solo CI publica.

### Cada release

1. Mergea el pull request de la release (`scripts/bump-version.sh X.Y.Z`, entrada en `docs/contract/CHANGELOG.md`).
2. Ensayo sobre `main`: `gh workflow run publish.yml -R cloudfroghq/knowledge --ref main` (y `release-check.yml`, sección 6) y `gh run watch`.
3. Solo la primera vez: el `npm publish` a mano y el trusted publisher de arriba.
4. Tag firmado sobre el merge y push (el ruleset `protect-release-tags` deja crear tags `v*` solo a administradores):

   ```bash
   git switch main && git pull --ff-only
   git tag -s v<x.y.z> -m "v<x.y.z>"
   git tag -v v<x.y.z>                           # firma válida con la llave de firma del repositorio
   git push origin v<x.y.z>
   gh run watch "$(gh run list -R cloudfroghq/knowledge -w publish -e push -L1 --json databaseId --jq '.[0].databaseId')" -R cloudfroghq/knowledge --exit-status
   ```

5. Comprueba los tres canales (en una máquina o un directorio sin caché caliente si quieres probar la descarga):

   ```bash
   uvx --managed-python --python 3.12 cloudfrog-knowledge@<x.y.z> --version                     # cloudfrog-knowledge <x.y.z>
   uvx --managed-python --python 3.12 --from cloudfrog-knowledge==<x.y.z> knowledge --version    # knowledge <x.y.z>
   npx -y @cloudfrog/knowledge@<x.y.z> --version                                                # knowledge <x.y.z>
   pnpm dlx @cloudfrog/knowledge@<x.y.z> --version
   uvx --managed-python --python 3.12 --from git+https://github.com/cloudfroghq/knowledge.git@v<x.y.z> cloudfrog-knowledge --version
   npm view @cloudfrog/knowledge@<x.y.z> dist.attestations                                       # procedencia (no la trae la primera versión)
   ```

### Si algo falla

- **Antes de publicar** (versiones, sumas de `uv`, tests, wheel): no salió nada. Se corrige en un pull request y se corta un tag nuevo.
- **PyPI rechaza el token** (`invalid-publisher`): el publicador no coincide con el dueño, repositorio, workflow o environment de la corrida. Corrígelo en PyPI y vuelve a correr el job: `gh run rerun <id> -R cloudfroghq/knowledge --failed`.
- **npm falla con PyPI ya publicado** (por ejemplo, el trusted publisher no estaba configurado): corrígelo y vuelve a correr el job fallido; PyPI responde `200` y se salta, y npm publica.
- **Una versión a medias en PyPI** (subió el wheel y no el sdist): una corrida nueva la ve publicada y se salta, así que el sdist ya no sube. `uv` y `pip` instalan desde el wheel; si el sdist hace falta, se publica un parche.
- **Una versión publicada con un defecto**: ni PyPI ni npm dejan volver a publicarla. Se corta un parche (`X.Y.Z+1`); en npm, `npm deprecate @cloudfrog/knowledge@<x.y.z> "<motivo>"`, y en PyPI se marca como *yanked* desde la página del proyecto.

## 8. Seguimiento

### Ruta del tag en `release-check`

Las únicas corridas de `release-check` son por `workflow_dispatch` (corrida 38362509479, en verde): la ruta del tag, que además compara el tag con las cuatro versiones, no está probada. Tras el siguiente push de un tag `v*`, registra la corrida por evento `push`:

```bash
gh run list -R cloudfroghq/knowledge -w release-check -e push -L1
```

Mientras no exista una corrida por tag, ese comando devuelve una lista vacía.

### Dependency Graph dinámico bajo `sha_pinning_required`

GitHub recalcula el grafo de dependencias de `uv.lock` con un workflow dinámico que el repositorio no versiona (`path: dynamic/dependabot/update-graph`, evento `dynamic`, job `update-uv-graph`, acción `github/dependabot-action@main`). No es un check de ningún ruleset.

```bash
gh api 'repos/cloudfroghq/knowledge/actions/runs?per_page=30' \
  --jq '.workflow_runs[]|select(.path|startswith("dynamic/"))|[.id,.created_at,.head_sha[0:7],.conclusion]|@tsv'
```

Lo que muestra el 2026-10-13:

- Son 12 corridas: 11 en verde (la última a las 02:08Z) y 1 en rojo a las 06:05Z (corrida 38354820108, commit `02e30f2`). Las 11 del 12 y 13 de octubre (UTC) coinciden cada una con un push a `main` cuyo lote incluye un commit que toca `pyproject.toml` o `uv.lock`; los cinco pushes posteriores a la corrida en rojo (11 commits) no tocaron ninguno de los dos y no generaron corrida.
- La corrida en rojo no falló por el pin. El job arrancó, descargó `github/dependabot-action@main` (resuelve al mismo SHA que en la corrida en verde) y ejecutó el updater. Falló al final: `POST /update_jobs/1624282216/create_dependency_submission` respondió `500` tras 10 s y el updater registró `snapshots_unavailable_graph_error: Unable to submit data to the Dependency Snapshot API`. En la corrida en verde esa llamada respondió `204`. El `GITHUB_TOKEN` del job tiene los mismos permisos en las dos (`Contents`, `Metadata` y `Packages` en lectura), así que `default_workflow_permissions: read` tampoco explica la diferencia.
- Eso no prueba que el workflow dinámico conviva con el pin. La API de permisos solo devuelve el estado actual, sin la fecha en que se activó `sha_pinning_required`, y la corrida en rojo precede en 40 minutos a la primera corrida de ring1 (06:45Z): lo más probable es que corriera sin el pin. No hay ninguna corrida dinámica posterior.

La prueba es la próxima corrida dinámica, que ya corre con el pin activo: llega con el siguiente push a `main` que cambie `pyproject.toml` o `uv.lock` (el bump de la siguiente release lo hace). Su log se lee con `gh api --allow-escape-sequences repos/cloudfroghq/knowledge/actions/jobs/<id del job>/logs`, porque la página de la actualización en la web pide acceso de escritura. Cómo decidir según el resultado:

- En verde: el grafo convive con el pin y no hay nada que cambiar.
- En rojo por un rechazo de la acción en `Set up job` (mensaje sobre acciones permitidas o sobre fijar por SHA): el workflow dinámico usa `github/dependabot-action@main`, una referencia que el repositorio no controla, y choca con `sha_pinning_required`. No se encontró documentación oficial que diga si los workflows de Dependabot quedan fuera de esa política y de `allowed_actions`, así que la decisión se toma con esta evidencia y no con una regla supuesta. El grafo es informativo y no protege `main`; el pin protege a ring1 y a release-check, que verifican cada merge y cada release. El criterio por omisión es conservar el pin y aceptar un grafo sin actualizar; apagarlo (la misma API de `actions/permissions` de la sección 1) solo se justifica si el grafo hace falta, y `tests/test_pipeline_contract.py` seguiría exigiendo SHA en los workflows versionados.
- En rojo con `snapshots_unavailable_graph_error` otra vez: es un error del servicio de GitHub, no del pin. Se reintenta con el siguiente push y, si persiste, se reporta a GitHub Support.
