# Promote Confluence pages into the vault

Runbook for whoever maintains a vault whose conventions originate in Confluence. The server does not write to any vault (D-11, [`docs/adr/ADR-044.md`](../adr/ADR-044.md)): `sync_confluence` only reports what is out of date, and `cloudfrog-knowledge confluence export` writes the pages outside the vault. Content enters the vault through a human PR with the owner as reviewer.

## 1. Credentials

The vault declares them in its `vault.yaml`, by variable name, never by value:

```yaml
integrations:
  confluence:
    site: https://acme.atlassian.net
    secrets: { email_env: ACME_CONFLUENCE_EMAIL, token_env: ACME_CONFLUENCE_TOKEN }
```

Whatever `integrations.confluence` does not declare comes from `CONFLUENCE_URL`, `CONFLUENCE_EMAIL` and `CONFLUENCE_TOKEN`. A value shaped like a credential inside `vault.yaml` is rejected with `E_SECRET_IN_MANIFEST`.

`site` and `CONFLUENCE_URL` are the site root, `https://<org>.atlassian.net`, **without** `/wiki`: the client appends `/wiki/api/v2/pages/<id>` on its own. With a trailing `/wiki` the server requests `/wiki/wiki/api/v2/...`, every page responds 404, and `confluence export` (like `sync_confluence`) leaves all of them in `errors` without writing any. The URL that `mcp-atlassian` uses (`https://<org>.atlassian.net/wiki`) does not work here.

The vault is curated content, so it may only point to Confluence variables that are its own: `site` must be `https` on `*.atlassian.net` or on a host the operator lists in `KNOWLEDGE_INTEGRATION_SITES` (`http` only on `127.0.0.1`, `localhost` or `::1`), and `email_env` and `token_env` must be uppercase, contain `CONFLUENCE` as a segment, end in `_EMAIL` and `_TOKEN`, and start with the vault's `slug` in uppercase (`ACME_CONFLUENCE_TOKEN` for `acme`). Only the default vault may use the unprefixed names (`CONFLUENCE_TOKEN`), and a vault that is not the default and declares `site` must name both variables. Any other name, such as `MCP_ADMIN_KEY`, `OPENAI_API_KEY` or another vault's variable, stops startup and `confluence export` with `E_MANIFEST_INVALID` and the key path, before any variable is read: this way no secret of the server or of another vault travels to the site a vault declares.

## 2. See what is out of date

From an MCP client, `sync_confluence()` (with `dry_run=True`, the default) lists in `needs_update` the files under `scope_prefix` (`Conventions/` by default) whose page is newer. `sync_confluence(dry_run=False)` responds `E_WRITE_FENCED` and neither reads nor writes anything.

## 3. Export outside the vault

```bash
cloudfrog-knowledge confluence export --vault acme --out ~/confluence-export/acme --dry-run
cloudfrog-knowledge confluence export --vault acme --out ~/confluence-export/acme
```

- Each out-of-date file lands at `<out>/<path relative to the vault>`, with the vault's frontmatter and `last_updated` set to the day the page changed.
- An `--out` inside any served vault, or a file that resolves inside one through a symlink, ends with `E_WRITE_FENCED` (exit code 2) without writing.
- Without credentials, `E_SECRET_MISSING` names the missing variables (exit code 2).
- The output is JSON with `written`, `unchanged` and `errors`; with any error the exit code is 1. A page that cannot be read, converted or written lands in `errors` as `{confluence_id, file, error}` and the rest are exported anyway.
- A filesystem failure outside a specific page (reading the vault, for example) ends with `E_EXPORT_FAILED` (exit code 2) and a single line on stderr.

## 4. Promote through a PR

1. In the checkout of the vault's repository: `git switch -c docs/confluence-<date>`.
2. Copy the files listed in `written` from `<out>` to the same path in the vault and review the diff: the body comes from a basic HTML-to-Markdown conversion, so tables and macros may need touching up.
3. `cloudfrog-knowledge vault-lint <vault>` with no new errors.
4. Commit, push and open a PR with the vault owner as reviewer. When it is merged, the vault's webhook reindexes.
