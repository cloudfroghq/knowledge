# Getting started

This runs the server from a clone against a copy of the test vault in `tests/fixtures/vault/`. It needs no network after `uv sync` and no API key: the `bag-of-words` embeddings provider is deterministic and local.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (the only hard requirement). The project pins Python 3.12 (`.python-version`, and `requires-python = ">=3.12,<3.13"`). Python 3.13 is not supported; if your system Python is 3.13, let `uv` provision 3.12 (it does so automatically, or with `uv python install 3.12`).
- `git`, and `curl` plus `openssl` for the HTTP section.
- If your shell exports an empty `UV_NATIVE_TLS=`, unset it: every `uv` call fails with "expected a boolish value".

```bash
git clone https://github.com/cloudfroghq/knowledge.git
cd knowledge
uv sync    # creates .venv with Python 3.12 and the dev tools
```

## Serve the fixture vault over stdio

```bash
DEMO="$(mktemp -d)"
cp -r tests/fixtures/vault "$DEMO/vault"     # never point the server at a vault you care about to try it
export VAULT_PATH="$DEMO/vault" KNOWLEDGE_INDEX_DIR="$DEMO/index" KNOWLEDGE_EMBEDDINGS_PROVIDER=bag-of-words
{
  printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"quickstart","version":"0"}}}' \
    '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
    '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search","arguments":{"query":"middleware outage","limit":3}}}' \
    '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_document","arguments":{"chunk_id":"d9bc623bc4:1","max_chars":300}}}' \
    '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"list_vaults","arguments":{}}}'
  sleep 5
} | uv run cloudfrog-knowledge serve --transport stdio 2>/dev/null
```

The `sleep` keeps stdin open while the server answers. Without it the pipe closes right after the last request, and the server can shut down before it writes the replies. `2>/dev/null` hides the log; drop it to see the indexing lines.

## What you should see

Five JSON-RPC responses, one per line (the notification gets none), not necessarily in request order:

- `initialize`: `instructions` starts with `knowledge-contract: 2.0.0`. `serverInfo.version` is the version of the `mcp` SDK, not of this package.
- `tools/list`: the 10 core tools, in this order: `get_document`, `search`, `search_by_metadata`, `search_deep`, `find_path`, `get_related`, `get_impact`, `reindex`, `context_pack`, `list_vaults`.
- `search`: one text block per row, for example `{"file_path":"Runbooks/Runbook - Middleware Outage.md","score":1.06,"chunk_id":"d9bc623bc4:1","section":"Runbook - Middleware Outage"}`, then `Architecture/Auth Architecture.md` (with `needs_review` and `age_days`) and `Services/Middleware Service.md`. Scores can vary slightly between runs.
- `get_document`: that chunk only: `file_path`, `metadata` (`type: runbook`, `service: middleware`, ...), `content` starting with `# Runbook - Middleware Outage`, `chunk_id` and `section`. Chunk ids are stable for unchanged content, which is why the recipe can hard-code the one `search` returns.
- `list_vaults`: `{"contract":"2.0.0","default":"vault","vaults":[{"slug":"vault","name":"vault","default":true,"features":[]}]}`.

The first `tools/call` builds the index: the log says that no existing index was found and runs the initial indexing, then `Wrote 14 documents (14 chunks)`. `tools/list` and `list_vaults` do not trigger it. `$DEMO/index` then holds `index.sqlite`, `index_meta.json`, `link_graph.json` and `usage_stats.json`. The warning `W_REVIEW_THRESHOLDS_FALLBACK` in the log is harmless: the fixture declares no review thresholds.

Two more things to try:

```bash
uv run cloudfrog-knowledge doctor --brief                     # knowledge doctor: ok, contract 2.0.0, 1 vault(s), default vault
uv run cloudfrog-knowledge vault-lint "$DEMO/vault" --rules 1.2   # ok: true, rc 0
```

`vault-lint` with its default `1.3` rules exits with 1 on the fixture on purpose: the fixture reproduces hard cases (an invalid runbook `status`, an old-style `last_updated`, and so on).

Without `VAULT_PATH`, `KNOWLEDGE_VAULTS` or a user `vaults.yaml`, startup fails with `E_VAULT_PATH_MISSING` (exit code 2). `KNOWLEDGE_EMBEDDINGS_PROVIDER=none` also works, with lexical search only.

## Connect it to an MCP client

**Claude Code, with the plugin (recommended).** The `cloudfroghq` marketplace publishes `knowledge@cloudfroghq` (stdio over your local vaults) and `knowledge-remote@cloudfroghq` (a deployed HTTP server). Install steps, prerequisites (including the minimum Claude Code version) and the vault registry the plugin reads are in [`docs/install.md`](install.md):

```bash
claude plugin marketplace add cloudfroghq/claude-plugins && claude plugin install knowledge@cloudfroghq --scope user
```

**Claude Code, by hand.** Register the published package over stdio:

```bash
claude mcp add -s user knowledge -e VAULT_PATH=/path/to/your/vault -- \
  uvx --managed-python --python 3.12 cloudfrog-knowledge serve --transport stdio
```

**Any MCP client (stdio).** The same server as a JSON entry; add `OPENAI_API_KEY` to `env` if you keep the default `openai` provider, or set `KNOWLEDGE_EMBEDDINGS_PROVIDER` to `none`:

```json
{
  "mcpServers": {
    "knowledge": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--managed-python", "--python", "3.12", "cloudfrog-knowledge", "serve", "--transport", "stdio"],
      "env": {
        "VAULT_PATH": "/path/to/your/vault",
        "KNOWLEDGE_EMBEDDINGS_PROVIDER": "none"
      }
    }
  }
}
```

To run a local checkout instead of the published package, use `"command": "uv"` with `"args": ["run", "--directory", "/path/to/knowledge", "cloudfrog-knowledge", "serve", "--transport", "stdio"]`. `python -m knowledge.server` is still a valid entry point, but on its own it just waits for JSON-RPC on stdin.

The first start can take longer than the 30 s an MCP client waits by default (it downloads the package and, if needed, a managed Python). Run the command once by hand to warm the cache, or raise `MCP_TIMEOUT` in Claude Code.

## Run over HTTP

```bash
export VAULT_PATH="$DEMO/vault" KNOWLEDGE_INDEX_DIR="$DEMO/index" KNOWLEDGE_EMBEDDINGS_PROVIDER=bag-of-words
export MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT=8765
export MCP_API_KEY="$(openssl rand -hex 32)"
uv run cloudfrog-knowledge serve --transport http &      # about 5 s to come up

curl -sS http://127.0.0.1:8765/health                    # 200, no bearer needed

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"quickstart","version":"0"}}}'
curl -i -X POST http://127.0.0.1:8765/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -d "$INIT"                       # 401 {"error":"unauthorized"}
curl -i -X POST http://127.0.0.1:8765/mcp -H "Authorization: Bearer $MCP_API_KEY" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$INIT"   # 200, mcp-session-id header
kill %1
```

- `MCP_HTTP_HOST` defaults to `0.0.0.0`, which binds every interface. Set `127.0.0.1` for anything local.
- `--transport http` is the same as `MCP_TRANSPORT=http` (the default is `stdio`). Port and path default to `8000` and `/mcp`.
- Over HTTP the server refuses to start without a bearer: `E_HTTP_API_KEY_MISSING` (exit 2) unless `MCP_API_KEY`, `MCP_ADMIN_KEY` or `bearers` in `vaults.yaml` is set.
- Responses are server-sent events (`event: message` / `data: {...}`). Every later request carries the `mcp-session-id` header from the `initialize` response, and the session starts with a `notifications/initialized` POST (202).
- `/health` before the first tool call shows `indexed: 0` and `index_loaded: false`; after a search, `indexed: 14` and `index_loaded: true`. `embedding_model` shows `text-embedding-3-large` even with `bag-of-words` or `none` (cosmetic).
- `/webhook/reindex` answers 404 here because the vault declares no repository.
