# Configuring MCP servers at user scope

This repository, the context packages that each vault keeps in `Contexts/`, and the
service checkouts that link or copy them no longer declare shared MCP servers in their
`.mcp.json`. Only what truly varies per repository remains there: in this one,
`graphify`, because the path of its graph is relative to the checkout, and `memory`,
because the tool profile and the project are decided per repository.

Everything else —`cloudfrog-knowledge`, `mcp-atlassian`, `newrelic-community`—
lives in Claude Code's user scope (`~/.claude.json`, `mcpServers` section),
fed by environment variables. This avoids declaring the same server once per context
and the scope conflict that appears when the same server name is defined in both
project scope and user scope with different contents.

`datadog-mcp` does not appear here: the user-scope instance already authenticates
through OAuth against Datadog and needs no variables or API keys.

## Required environment variables

None of them lives in this repository or in any versioned `.mcp.json`. Each
person defines them in a file outside any checkout, with `600` permissions,
loaded from their shell configuration.

| Variable | What it is for |
|---|---|
| `CD_KNOWLEDGE_URL` | URL of this server's MCP, used by the `cloudfrog-knowledge` server: `https://<deployment>/mcp`. Each deployment documents it in its vault, in `Deploy/`. |
| `CD_KNOWLEDGE_TOKEN` | Bearer token for that same MCP: the deployment's `MCP_API_KEY` (read-only) or the personal bearer assigned to you. |
| `ATLASSIAN_EMAIL` | Email of the Atlassian account used by `mcp-atlassian` (Jira and Confluence). |
| `ATLASSIAN_API_TOKEN` | API token of that Atlassian account. |
| `NEW_RELIC_API_KEY` | New Relic API key for `newrelic-community`. |
| `NEW_RELIC_ACCOUNT_ID` | New Relic account id associated with that API key. |

## Environment file template

Copy this to a file outside any repository (for example,
`~/.config/mcp/mcp-env.fish`), fill in the real values, and leave it with
`600` permissions:

```fish
# Variables consumed by the user-scope MCP servers.
# Contains credentials: 600 permissions, outside any repository.
set -gx CD_KNOWLEDGE_URL "https://YOUR-KNOWLEDGE-MCP-HOST/mcp"
set -gx CD_KNOWLEDGE_TOKEN "YOUR_TOKEN_HERE"
set -gx ATLASSIAN_EMAIL "your-email@yourcompany.com"
set -gx ATLASSIAN_API_TOKEN "YOUR_ATLASSIAN_API_TOKEN_HERE"
set -gx NEW_RELIC_API_KEY "YOUR_NEW_RELIC_API_KEY_HERE"
set -gx NEW_RELIC_ACCOUNT_ID "YOUR_NEW_RELIC_ACCOUNT_ID_HERE"
```

And load it from `~/.config/fish/config.fish` (adapt the path and the shell if you
do not use fish):

```fish
if test -r ~/.config/mcp/mcp-env.fish
    source ~/.config/mcp/mcp-env.fish
end
```

## Servers to declare at user scope

In `~/.claude.json`, inside `mcpServers` (user scope: it applies in
any repository and is not versioned with the project). Adjust `JIRA_URL` and
`CONFLUENCE_URL` to your organization's real Atlassian domain.
The `CONFLUENCE_URL` of `mcp-atlassian` ends in `/wiki`; the one for `cloudfrog-knowledge` (`sync_confluence`, `confluence export`) is the site root, without `/wiki`, and is not copied from here.

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
      "JIRA_URL": "https://YOUR-DOMAIN.atlassian.net",
      "JIRA_USERNAME": "${ATLASSIAN_EMAIL}",
      "JIRA_API_TOKEN": "${ATLASSIAN_API_TOKEN}",
      "CONFLUENCE_URL": "https://YOUR-DOMAIN.atlassian.net/wiki",
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

The `--python=3.12` pin on `mcp-atlassian` is deliberate: always declare it, never
a bare `uvx`.

## `memory` stays per repository, with the `projects` profile

The `mem_*` tools arrive through two paths that do not overlap. The
`memory@cloudfroghq` Claude Code plugin (its tools arrive as
`mcp__plugin_memory_memory__*`; the old `cloudfrog@cloudfrog` plugin uses a
retired prefix and migrating to `memory@cloudfroghq` is recommended, see `docs/install.md`)
provides session memory —`mem_save`, `mem_search`, `mem_context`,
`mem_get_observation` and the rest of the `agent` profile—, is installed from the
`cloudfroghq` marketplace (`docs/install.md`) and does not need to be declared in any
`.mcp.json`. The `memory` server
that this repository does declare provides the ten of the `projects` profile:
`mem_project_card`, `mem_project_upsert`, `mem_task_upsert`, `mem_task_list`,
`mem_task_link`, `mem_evidence_add`, `mem_evidence_list`,
`mem_runbook_index_sync`, `mem_runbook_find` and `mem_context_pack`. None of
those ten exists in the plugin, so declaring the server duplicates nothing.

```json
"memory": {
  "type": "stdio",
  "command": "${MEMORY_BIN:-memory}",
  "args": ["mcp", "--tools=projects"],
  "env": { "MEMORY_PROJECT": "knowledge" }
}
```

The profile is `projects` and nothing else. A per-profile `tools/list` measures it:
`agent` (18) and `admin` (4) are exactly the plugin's 22 tools, and `all` (39)
contains all of them, so any of those three sets up the same write
under two servers. `projects` (10) and `workspace` (10) repeat none, but
of the two only `projects` brings what this repository's skills invoke.
Enumerate the profile with a real `tools/list` before widening it.

`MEMORY_PROJECT` is fixed to `knowledge` because that is the project the plugin
resolves from the git remote in this checkout. If the server pointed at another
name, plugin and server would write to different projects and the history
would be split in two. `mem_current_project` answers which project is
active.

`MEMORY_BIN` accepts an override; its default is the name `memory`, which `PATH`
resolves. An absolute path is not used: a dangling symlink to a checkout
that is no longer there passes any `test -e` and then kills the server with an opaque
`ENOENT`.

## Verification after setting this up

```bash
claude mcp list
```

It must connect with no warnings about missing variables and no "conflicting scopes",
both inside a repository with the reduced `.mcp.json` and outside any
repository.

For `memory`, the check that counts is the handshake, not a `--version`:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | "${MEMORY_BIN:-memory}" mcp --tools=projects
```

`tools/list` must return exactly the ten tools of the `projects` profile.
