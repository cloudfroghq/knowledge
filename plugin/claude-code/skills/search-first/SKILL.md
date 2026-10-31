---
name: search-first
description: Reading protocol of the knowledge server — search and read only the section the row points to.
when_to_use: before answering about architecture, flows, runbooks, API contracts or conventions that live in a vault, and whenever you are about to open a vault document.
allowed-tools: mcp__plugin_knowledge_knowledge__search, mcp__plugin_knowledge_knowledge__get_document, mcp__plugin_knowledge_knowledge__search_by_metadata, mcp__plugin_knowledge_knowledge__context_pack, mcp__plugin_knowledge_knowledge__list_vaults
user-invocable: true
argument-hint: <question>
---

# search-first

Find first, then read one section. Never page through a whole file.

## Protocol

1. Search. Each row already brings `file_path`, `score`, `chunk_id` and `section` (the block it points to), with no excerpts:

   ```
   search(query="$ARGUMENTS")
   ```

2. Pick the row with the best `score` whose `section` answers the question and read only that block:

   ```
   get_document(chunk_id=<chunk_id>)
   ```

   The `chunk_id` is enough: `file_path` is not needed, and the read is cut at 3,000 characters. If the row brings `chunk_id: null` (no term of the query appears in the file), read `get_document(file_path=<file_path>)`. If the block is not enough, read the neighboring `chunk_id` or the next `search` row; do not raise `max_chars` to bring in the whole document.

3. If the task asks for context from several sources in a single call, use `context_pack(query, budget_tokens=3000)` instead of chaining reads.

4. To filter by metadata (type, owner, tag, status) use `search_by_metadata` before a semantic search. It answers `{total, offset, items}` with 20 rows per page: read `items` and ask for the next page with `offset` while `offset + len(items) < total`.

5. With several vaults registered, `list_vaults` says which ones there are and which is the default; pass `vault=<slug>` to any tool to query another one.

6. If the vault declares a judge (the `rerank` block of its `vault.yaml`), `search` can rerank the rows with it: with `rerank.default: on` it does so without being asked; with `default: off`, pass `rerank="on"`. Reranked rows carry `rerank_score` (the probability that the block answers) and their `chunk_id` and `section` name the block the judge read: read that one. `rerank="off"` avoids it. `rerank="on"` on a vault with no judge answers `E_RERANK_NOT_CONFIGURED`: omit the argument.

## Rules

- `file_path` is relative to the vault: pass it as is, with no prefixes or wikilink brackets.
- Every error carries a stable `E_` code followed by an actionable message (sometimes after the `Error executing tool …:` prefix): fix the argument it names, do not retry the same way.
- Cite the `file_path` and the `section` that each claim comes from.
- If nothing in the vault answers, say so; do not fill in with guesses about another service's code.
- If the question is how something works in a checkout's code and it names no identifier, use the `code-question` skill (`jg` over a narrow folder), never over the vault.
- For questions about code there is no knowledge tool (`search_code` was retired in contract 2.0.0): use the `code-question` skill (`jg`, graphify, `rg`).
- `get_document` returns in `metadata` only `type`, `status`, `service`, `owner`, `tags` and `last_updated`; ask for `full_metadata=true` if you need another frontmatter key.
