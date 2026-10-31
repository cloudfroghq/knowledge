# Retrieval evaluation

A **golden set** is a list of real queries together with the documents that answer them, labeled by reading the documents, never by copying what the search engine returns (that would make the measurement circular). `scripts/eval_retrieval.py` runs it against a server through the tool contract — one `search` call per query — and publishes recall@k, precision@k, MRR@k and tokens per query.

The sets and the measurements are content of each vault, not of the server: each vault keeps its own in `Eval/` (`Eval/queries.yaml`, and `Eval/Results/` for the reports), and each measurement names the exact vault commit it ran against. This repository keeps only the fixture's set:

| Set | Vault | Purpose |
|---|---|---|
| `tests/fixtures/eval/queries.yaml` | `tests/fixtures/vault/` | Exercises the harness in the suite (`tests/test_eval_retrieval.py`), with the bag-of-words embeddings double. Its numbers say nothing about real quality. |

Whoever edits a vault's set checks that all the paths exist and that each `answer_terms` entry appears in the main document (the suite does not do this: no test reads a real vault). The command starts no server and uses no key:

```bash
uv run python scripts/eval_retrieval.py --check-labels --vault "$VAULT" --queries "$VAULT/Eval/queries.yaml"
```

## Format

```yaml
version: 1
vault: acme               # slug of the vault the paths point to
k: 5                      # metrics cutoff and `limit` of each search
queries:
  - id: acme-001          # unique within the set
    lang: es              # es | en
    query: "¿Cómo resuelve el servicio el tenant de una petición?"
    expected:             # 1 to 3 paths relative to the vault root, the main one first
      - "Architecture/Tenants.md"
    answer_terms:         # optional, 2 to 4 literals from the passage of expected[0] that answers the question
      - "TenantResolver"
      - "X-Tenant-ID"
    note: "why that document answers"   # optional
```

`answer_terms` are copied from the document, like `expected`, never from the server's output. A read "covers" the query when it contains all the terms; this measures whether the reader obtained the answer without depending on which tool or which chunking produced the read. It is an optional key within `version: 1`: the loader ignores unknown per-query keys and reads this one if it exists.

Paths go without a vault prefix and without wikilink brackets, like `file_path` in the server's responses. `paraphrase:` notes mark queries that do not repeat the words of the title (the lexical-mismatch case) and `cross-lingual:` marks those that ask in one language for a document written in the other.

### Control set

When a tuning (the `search` rerank, a hyperparameter sweep) is chosen by measuring variants on a set, measuring it only there reproduces the tuning, it does not validate it. To validate it you need a separate set, frozen without any run with the tuning having read it: different queries, no main document in common with the tuning set and, next to the file, its `sha256` recorded in the report that backs the numbers. If a label turns out to be wrong, it is corrected in a new set with its own hash, never in the frozen one.

## How to run it

The command is a script and not a `cloudfrog-knowledge` subcommand: the entry point imports `knowledge.server`, and that import runs `config.validate()`, which requires `OPENAI_API_KEY` and `VAULT_PATH` even to talk to a remote server. In addition, the tokenizer (`tiktoken`) and `pyyaml` are development dependencies and must not ship in the distribution. When the entry point moves to `knowledge.cli` with the configuration out of the import, `cloudfrog-knowledge eval` wraps this same script.

Against a local stdio server (it indexes the vault in a temporary directory; the key is injected through the environment only, never in a file or an argument):

```bash
OPENAI_API_KEY="$(op item get OPENAI_API_KEY --fields api_key --reveal)" uv run python scripts/eval_retrieval.py --stdio --vault "$VAULT" --queries "$VAULT/Eval/queries.yaml" --json build/eval/report.json
```

Without a key, with a provider that does not need one (`none` or `bag-of-words`). `--index-dir` sets the index directory (otherwise a temporary one) and `--extra-env KEY=VALUE`, repeatable, adds a variable to the server's environment; `scripts/measure_tokens.py --flow` accepts the same three options:

```bash
uv run python scripts/eval_retrieval.py --stdio --provider none --vault "$VAULT" --queries "$VAULT/Eval/queries.yaml" --index-dir build/eval/index-none --json build/eval/none.json
```

Against an HTTP deployment (URL and bearer through the environment only; the report does not contain them):

```bash
KNOWLEDGE_EVAL_URL=… KNOWLEDGE_EVAL_BEARER=… uv run python scripts/eval_retrieval.py --http --queries "$VAULT/Eval/queries.yaml" --json build/eval/http.json --markdown build/eval/http.md
```

With the vault's judge (`rerank` in its `vault.yaml`), `--rerank on|off` sends that argument in every `search` (without the option, it is not sent and `rerank.default` rules). The judge's key reaches the server with `--pass-env NAME`, which copies the variable from the current environment by its name: the value never appears on the command line, where it would with `--extra-env`. `--cover` asks in every `search` for the fields of the read protocol (`file_path`, `chunk_id`, `section`, `score`) and, once the last search is done, reads the first row of each query with `get_document(file_path, chunk_id=…, max_chars=2000)`; the reads go last so that the usage they record does not move any measured ranking. `scripts/measure_tokens.py --flow` accepts `--rerank` and `--pass-env` the same way:

```bash
OPENAI_API_KEY=… ACME_TYPESAFE_API_KEY=… uv run python scripts/eval_retrieval.py --stdio --vault "$VAULT" \
  --queries "$VAULT/Eval/queries.yaml" --index-dir build/eval/idx-rr-on \
  --pass-env ACME_TYPESAFE_API_KEY --rerank on --cover --json build/eval/rr-on.json
```

The JSON report stores, per query, the `_meta["knowledge/rerank"]` the server returned, counts the searches the judge reordered and why it was skipped in the others and, with `--index-dir`, copies the `rerank` section of the index's `usage_stats.json` (billed tokens, USD, judge latency buckets): the cost per query comes from there.

Each `search` records usage on the server that receives it (the bounded ranking boost and the co-retrieval pairs). Running the set against a live deployment moves its ranking a little; repeating it many times in a row biases its own numbers.

## Metrics

- **recall@k**: fraction of the expected documents that appear in the top k.
- **precision@k**: expected documents in the top k divided by k. With 1 to 3 expected per query the maximum achievable is well below 1; the report prints that ceiling next to it. (The Markdown report currently labels it `precisión@k`.)
- **MRR@k**: inverse of the rank of the first expected document within the top k (0 if it does not appear).
- **hit rate@k**: fraction of queries with at least one expected document in the top k. (The Markdown report currently labels it `acierto@k`.)
- **hit@1** and **prim@1**: fraction of queries whose first row is an expected document, and whose first row is the main expected document (`expected[0]`).
- **nDCG@k**: binary relevance, each expected document in the top k discounted by `log2(rank + 1)` and normalized by the best possible order of `min(expected, k)` documents.
- **cover@1** (with `--cover`): fraction of the queries with `answer_terms` whose read of the first row with `get_document(chunk_id)` (the `instructions` protocol, 2,000 characters) contains all the terms.
- **tokens per query**: the text blocks of the `search` response (what enters the model's context), counted with tiktoken `cl100k_base`. It is a proxy: Claude's tokenizer is not public.
