# @cloudfrog/knowledge

CloudFrog's curated-knowledge MCP server (`cloudfrog-knowledge`) and its vault tooling (`doctor`, `init`, `vault-lint`, ...), for people who prefer to install with npm. The package ships no Python: on the first run it downloads a pinned version of [`uv`](https://github.com/astral-sh/uv), checks its SHA-256 against the checksum bundled in the package, and uses that `uv` to run the release of `cloudfrog-knowledge` published on PyPI with the same version as this package.

```bash
npx @cloudfrog/knowledge --version
pnpm dlx @cloudfrog/knowledge doctor
npm install -g @cloudfrog/knowledge && knowledge doctor
```

The two commands, `knowledge` and `cloudfrog-knowledge`, are the same program. With no subcommand it runs `serve`, the MCP server over stdio. Requires Node.js 18 or later; the package has no npm dependencies.

## Three ways to install it

| Channel | Once | Each use |
|---|---|---|
| PyPI with `uv` | `uv tool install --managed-python --python 3.12 cloudfrog-knowledge` | `knowledge ...` (or without installing: `uvx --managed-python --python 3.12 cloudfrog-knowledge ...`) |
| npm (this package) | `npm install -g @cloudfrog/knowledge` | `knowledge ...` (or without installing: `npx @cloudfrog/knowledge ...`, `pnpm dlx @cloudfrog/knowledge ...`) |
| Git tag | | `uvx --managed-python --python 3.12 --from git+https://github.com/cloudfroghq/knowledge.git@vX.Y.Z cloudfrog-knowledge ...` |

All three run the same code: this package's version is the PyPI version and the tag (`vX.Y.Z` stands for the release you want). `--python 3.12` matters because `uv` ignores the upper bound of `requires-python` when it picks an interpreter; this package always passes it. The full guide, with the Claude Code plugin and the vaults, is in [`docs/install.md`](https://github.com/cloudfroghq/knowledge/blob/main/docs/install.md); the server itself is described in the [repository README](https://github.com/cloudfroghq/knowledge#readme).

## As an MCP server

Pin the version (`X.Y.Z`, this package's version) so that every start runs the same release:

```json
{
  "mcpServers": {
    "knowledge": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@cloudfrog/knowledge@X.Y.Z", "serve", "--transport", "stdio"],
      "env": { "VAULT_PATH": "/path/to/your/vault" }
    }
  }
}
```

The server needs a vault: `VAULT_PATH`, `KNOWLEDGE_VAULTS` (a `vaults.yaml`) or `~/.config/knowledge/vaults.yaml`. With the default `openai` embeddings provider it also needs `OPENAI_API_KEY`; `KNOWLEDGE_EMBEDDINGS_PROVIDER=none` runs lexical search without a key. See the [configuration reference](https://github.com/cloudfroghq/knowledge#configuration).

The first start downloads `uv`, a `uv`-managed CPython 3.12 (if there is none) and the package's dependencies: it can take longer than the 30 s an MCP client waits by default. Run it once by hand (`npx @cloudfrog/knowledge@X.Y.Z doctor`) to warm the cache. The wrapper never writes to stdout: everything it prints before the CLI starts goes to stderr, so an MCP client talks to the server directly.

## What it does on your machine

- It picks the `uv` release archive for your platform: macOS arm64 and x64, Linux x64 and arm64 (glibc or musl), and Windows x64 and arm64. Any other platform ends with `E_UV_UNSUPPORTED_PLATFORM`.
- It downloads it from `https://github.com/astral-sh/uv/releases/download/<version>/`, compares its SHA-256 with the one the package carries (`lib/uv-release.json`; nothing is looked up online for that) and, if they do not match, installs nothing (`E_UV_CHECKSUM_MISMATCH`).
- It stores it in `~/.cache/cloudfrog-knowledge/uv/<version>/` (under `$XDG_CACHE_HOME` when you set it to an absolute path; `%LOCALAPPDATA%\cloudfrog-knowledge\uv\<version>\` on Windows). The install is atomic, and a lock keeps two simultaneous first starts from downloading twice; later runs do not touch the network for `uv`.
- It runs `uv tool run --managed-python --python 3.12 --from cloudfrog-knowledge==<version> <command> <arguments>`, with the same stdin, stdout and stderr, and exits with the CLI's exit code. Signals the wrapper receives are passed on to the CLI.

| Variable | What it is for |
|---|---|
| `CLOUDFROG_KNOWLEDGE_UV` | Path of a `uv` you already have. With it nothing is downloaded; if it is not a file, the wrapper exits with `E_UV_OVERRIDE_INVALID`. Useful behind a proxy that Node cannot get through. |
| `KNOWLEDGE_COMMAND_LABEL` | The command that the hints of `doctor` and `init` name (by default, the one you ran: `knowledge` or `cloudfrog-knowledge`). Read by the CLI, not by the wrapper. |

A wrapper failure before the CLI starts (`E_UV_DOWNLOAD_FAILED` without a network, for example) prints a single `<code>: <message>` line on stderr and exits with 2.

## License

[MIT License](https://opensource.org/license/mit): see `LICENSE`.
