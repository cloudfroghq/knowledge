---
paths: ["src/knowledge/server.py"]
---

# API design (the tool contract is this service's API)

- `file_path` is **always** returned without a prefix and without wikilink brackets.
- No response shape changes without a tool version bump and without regenerating the server's tool inventory (`docs/tools/inventory.md`).
- A new tool is documented before it is implemented.
- Every mutating tool (`reindex`, `sync_confluence`) requires an `admin`-scope token.
- The server never writes to the vault outside `90 - Memory/`.
- `include_memories=false` is the default, and the fence is tested, not assumed.
- Errors are returned with a stable code and an actionable message, never with the Python traceback.
- No error message includes the bearer, the OpenAI key or the server's absolute paths.
- The indexer reads frontmatter but does not validate it (there is no `schema/`); a file that cannot be indexed is reported in `skipped_files.json` with its reason, never dropped silently. Frontmatter rules are checked by `vault-lint`.
