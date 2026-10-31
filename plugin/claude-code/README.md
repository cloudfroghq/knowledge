# `knowledge` plugin for Claude Code

Serves the curated-knowledge MCP server over your local vaults through stdio. Claude Code starts it with `uvx`, which installs the `cloudfrog-knowledge` release from PyPI named in `.mcp.json`. Install it from the `cloudfroghq` marketplace:

```bash
claude plugin marketplace add cloudfroghq/claude-plugins
claude plugin install knowledge@cloudfroghq --scope user
```

The full team guide, with prerequisites and cache warm-up, is in [`docs/install.md`](../../docs/install.md).

## What it ships

| Piece | What it does |
|---|---|
| `.mcp.json` | The `knowledge` server: `uvx --managed-python --python 3.12 --from cloudfrog-knowledge==<version> cloudfrog-knowledge serve --transport stdio`. Its tools arrive as `mcp__plugin_knowledge_knowledge__<tool>` and the server appears in `/mcp` as `plugin:knowledge:knowledge`. |
| `hooks/hooks.json` | A `SessionStart` hook that runs `cloudfrog-knowledge doctor --brief` with the same package and options as the server. It prints nothing if everything is fine; if something fails, it prints a single line of up to 200 bytes, not counting the newline (the cap of `doctor --brief`). With a healthy doctor it also speaks up when a served vault declares `Contexts/config.json` and this machine has not initialized it: `knowledge doctor: init pending for vault(s) <slugs>; run: cloudfrog-knowledge init`. If the doctor does not answer within 8 seconds (cold `uv` cache) it also stays silent. If `uvx` fails without the doctor saying anything (no network, or a release PyPI does not have), it warns once per day and per plugin version, not on every startup. The only thing the hook writes to disk is the marker of that warning (see Diagnostics). With a healthy doctor it also launches `cloudfrog-knowledge jira sync --auto` in the background, detached from the hook (the hook neither waits for it nor shows its output): it syncs the vaults that declare `jira`, have their variables set and have already passed `sync.throttle_minutes`, and writes their tasks to the local memory and the cursor next to the index. `KNOWLEDGE_JIRA_AUTOSYNC=0` turns it off (`docs/runbooks/sync-jira.md`). |
| `skills/search-first` | Reading protocol: `search` with pointer fields and a single-section `get_document`. |
| `skills/repair-mcp-tool` | Repair of a broken tool, with its regression test in red first. |
| `skills/code-question` | Conceptual question about code without a known identifier: `jg` (jevgrep) over a narrow folder with `--max-source-bytes 20000`, never over a vault; counts, symbols and impact stay with `rg` and graphify. |

## Options (`userConfig`)

| Option | Variable the server receives | When empty |
|---|---|---|
| `vaults_config` (file) | `KNOWLEDGE_VAULTS` | The server uses `~/.config/knowledge/vaults.yaml` (or `$XDG_CONFIG_HOME/knowledge/vaults.yaml`). |
| `embeddings_provider` (`openai`, `none` or `bag-of-words`) | `KNOWLEDGE_EMBEDDINGS_PROVIDER` | `vaults.yaml` decides (`openai` by default). |

Claude Code exports an option with no value as an empty string, and the server treats an empty string as an absent variable: leaving both options empty is the normal case.

To save them: `/plugin` → `Installed` → `knowledge` → `Configure` inside the session, or `claude plugin configure knowledge@cloudfroghq --values-stdin < values.json`. Claude Code must be restarted afterwards.

## What the plugin does not define, and why

- **`KNOWLEDGE_HOME`**: it takes precedence over the `data_dir` of `vaults.yaml`. Pointing it at the plugin's data directory (`${CLAUDE_PLUGIN_DATA}`) would make uninstalling or reinstalling the plugin delete the indexes and force every vault to be embedded again. The indexes live where your `vaults.yaml` says (`data_dir`) or, without that, in `~/.local/share/knowledge`.
- **`OPENAI_API_KEY`**: the server inherits it from the environment Claude Code starts with. If `.mcp.json` mapped it from an option with no value, Claude Code would export an empty string on top of the inherited variable and startup would fail with `E_EMBEDDINGS_KEY_MISSING`. If your session does not inherit the key, `cloudfrog-knowledge doctor` reports it with that same code.

## Optional environment variables

| Variable | What for |
|---|---|
| `UVX_BIN` | Absolute path to `uvx` when Claude Code does not inherit your `PATH` (for example, when opened from the Dock). By default, `uvx` resolved through `PATH`. |
| `KNOWLEDGE_DOCTOR_BUDGET` | Seconds the hook waits for the doctor (8 by default). |
| `XDG_CACHE_HOME` | Base of the hook's daily-warning marker (`knowledge/session-start-package-failed`). By default, `$HOME/.cache`. |
| `MCP_TIMEOUT` | Milliseconds Claude Code waits for an MCP server to start. The first startup with a cold cache can exceed the default 30 s: `60000` gives some margin. |

## Diagnostics

```bash
uvx --managed-python --python 3.12 --from 'cloudfrog-knowledge==X.Y.Z' cloudfrog-knowledge doctor
```

`X.Y.Z` stands for the plugin's version (`version` in `plugin.json`; the same release the plugin's `.mcp.json` pins). `scripts/bump-version.sh` does not update the docs, so the version is written as a placeholder here.

It prints a JSON with the discovery rule that won, the registry file, and per vault the directory, the provider and whether its key is defined (never the value), whether `KNOWLEDGE_HOME` is writable, and the package and contract versions. It exits 0 if everything is fine, 1 with problems and 2 if the configuration does not load.

The session-start hook does not repeat the same failure on every startup. When `uvx` cannot even start the package (no network, or a tag the remote does not have), it prints `knowledge: server package failed (uvx rc N); diagnose: …` once per day and per plugin version. The marker is the one-line file `${XDG_CACHE_HOME:-$HOME/.cache}/knowledge/session-start-package-failed`: deleting it repeats the warning on the next startup, and if it cannot be written, the warning appears on every startup. Meanwhile, `/mcp` shows the server as `failed`.

## Version

`version` in `plugin.json`, the `cloudfrog-knowledge==<version>` in `.mcp.json` and `version` in `pyproject.toml`, `version` in `npm/package.json` (plus the project in `uv.lock`) move together with `scripts/bump-version.sh X.Y.Z` and are checked with `scripts/check-version.sh [vX.Y.Z]`. The tag is cut after merging the release pull request, on that merge.
