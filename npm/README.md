# @cloudfrog/knowledge

El servidor MCP de conocimiento curado de CloudFrog (`cloudfrog-knowledge`) y sus herramientas de vault (`doctor`, `init`, `vault-lint`, …), para quien prefiere instalar con npm. El paquete no trae Python: en la primera corrida descarga una versión fija de [`uv`](https://github.com/astral-sh/uv), verifica su SHA-256 contra la suma que viene dentro del paquete y con ese `uv` corre la misma versión de `cloudfrog-knowledge` publicada en PyPI.

```bash
npx @cloudfrog/knowledge --version
pnpm dlx @cloudfrog/knowledge doctor
npm install -g @cloudfrog/knowledge && knowledge doctor
```

Los dos comandos, `knowledge` y `cloudfrog-knowledge`, son el mismo programa. Sin subcomando equivale a `serve`, el servidor MCP por stdio.

## Tres formas de instalarlo

| Canal | Una vez | Cada uso |
|---|---|---|
| PyPI con `uv` | `uv tool install --managed-python --python 3.12 cloudfrog-knowledge` | `knowledge …` (o sin instalar: `uvx --managed-python --python 3.12 cloudfrog-knowledge …`) |
| npm (este paquete) | `npm install -g @cloudfrog/knowledge` | `knowledge …` (o sin instalar: `npx @cloudfrog/knowledge …`, `pnpm dlx @cloudfrog/knowledge …`) |
| Tag de git | — | `uvx --managed-python --python 3.12 --from git+https://github.com/cloudfroghq/knowledge.git@v0.8.0 cloudfrog-knowledge …` |

Las tres corren el mismo código: la versión de este paquete es la de PyPI y la del tag. `--python 3.12` importa porque `uv` ignora el tope de `requires-python` al elegir intérprete; este paquete lo pasa siempre. La guía completa, con el plugin de Claude Code y los vaults, está en [`docs/install.md`](https://github.com/cloudfroghq/knowledge/blob/main/docs/install.md).

## Como servidor MCP

```json
{
  "mcpServers": {
    "knowledge": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@cloudfrog/knowledge@0.8.0", "serve", "--transport", "stdio"]
    }
  }
}
```

El primer arranque descarga `uv`, un CPython 3.12 gestionado por `uv` (si no hay uno) y las dependencias del paquete: puede pasar de los 30 s que un cliente MCP espera por omisión. Córrelo una vez a mano (`npx @cloudfrog/knowledge@0.8.0 doctor`) para dejar la caché caliente. El paquete nunca escribe en stdout: todo lo que imprime antes de arrancar el CLI va a stderr.

## Qué hace en tu máquina

- Elige el archivo de la release de `uv` de tu plataforma: macOS arm64 y x64, Linux x64 y arm64 (glibc o musl) y Windows x64 y arm64. Otra plataforma termina con `E_UV_UNSUPPORTED_PLATFORM`.
- Lo baja de `https://github.com/astral-sh/uv/releases/download/<versión>/`, compara su SHA-256 con el que trae el paquete (`lib/uv-release.json`; nada se consulta en línea para eso) y, si no coincide, no instala nada (`E_UV_CHECKSUM_MISMATCH`).
- Lo deja en `~/.cache/cloudfrog-knowledge/uv/<versión>/` (`$XDG_CACHE_HOME` si la defines; `%LOCALAPPDATA%\cloudfrog-knowledge\uv\<versión>\` en Windows). La instalación es atómica y un candado evita que dos arranques simultáneos descarguen dos veces; las corridas siguientes no tocan la red para `uv`.
- Ejecuta `uv tool run --managed-python --python 3.12 --from cloudfrog-knowledge==<versión> <comando> <argumentos>`, con la misma entrada y salida, y termina con el código del CLI. Una señal que recibe el paquete pasa al CLI.

| Variable | Para qué |
|---|---|
| `CLOUDFROG_KNOWLEDGE_UV` | Ruta de un `uv` que ya tienes. Con ella no se descarga nada; si no es un archivo, sale con `E_UV_OVERRIDE_INVALID`. Sirve detrás de un proxy que Node no atraviesa. |
| `KNOWLEDGE_COMMAND_LABEL` | El comando que nombran las pistas de `doctor` e `init` (por omisión, el que corriste: `knowledge` o `cloudfrog-knowledge`). |

Un fallo del paquete antes de arrancar el CLI (`E_UV_DOWNLOAD_FAILED` sin red, por ejemplo) imprime una sola línea `<código>: <mensaje>` en stderr y sale con 2.

## Licencia

[PolyForm Shield License 1.0.0](https://polyformproject.org/licenses/shield/1.0.0): ver `LICENSE`, incluida la línea `Required Notice:`.
