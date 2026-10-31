---
paths: ["src/knowledge/**", "tests/**"]
---

# Testing

- No test uses the production vault: everything runs against `tests/fixtures/vault/`.
- Every tool has at least one async test that invokes it through its real interface, and a schema test that freezes the response shape.
- A repair lands with its regression test in red first, using the parameters documented as failing today.
- Coverage thresholds live in `pyproject.toml`, never only in a document.
- The pipeline runs on Linux, not on macOS: macOS case-insensitivity has already hidden a real defect.
- The suite uses the real store: a `SQLiteStore` over a temporary directory, with FTS5 and `sqlite-vec`. Embeddings come from the deterministic `bag-of-words` provider (`KNOWLEDGE_EMBEDDINGS_PROVIDER=bag-of-words`); no test calls OpenAI or patches the store.
- `KNOWLEDGE_REQUIRE_SQLITE_VEC=1` turns the `python` fallback (the extension does not load in that Python) into the `E_SQLITE_VEC_UNAVAILABLE` error: the pipeline and the golden recording set it, and goldens are recorded only with `uv`'s Python 3.12.
- The RFC's contract suite is blocking and is updated together with the RFC, never afterwards.
