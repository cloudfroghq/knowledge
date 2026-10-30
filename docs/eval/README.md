# Evaluación de retrieval

Un **golden set** es una lista de consultas reales con los documentos que las responden, etiquetadas leyendo los documentos, nunca copiando lo que devuelve el buscador (eso haría circular la medición). `scripts/eval_retrieval.py` lo corre contra un servidor por el contrato de tools —una llamada `search` por consulta— y publica recall@k, precisión@k, MRR@k y tokens por consulta.

Los sets y las mediciones son contenido de cada vault, no del servidor: cada vault guarda los suyos en `Eval/` (`Eval/queries.yaml`, y `Eval/Results/` para los reportes), y cada medición nombra el commit exacto del vault contra el que corrió. Este repositorio solo conserva el set de la fixture:

| Set | Vault | Para qué |
|---|---|---|
| `tests/fixtures/eval/queries.yaml` | `tests/fixtures/vault/` | Ejercita el harness en la suite (`tests/test_eval_retrieval.py`), con el doble de embeddings bag-of-words. Sus números no dicen nada de la calidad real. |

Quien edite el set de un vault comprueba que todas las rutas existen y que cada `answer_terms` aparece en el documento principal (la suite no lo hace: ningún test lee un vault real). El comando no levanta ningún servidor ni usa clave:

```bash
uv run python scripts/eval_retrieval.py --check-labels --vault "$VAULT" --queries "$VAULT/Eval/queries.yaml"
```

## Formato

```yaml
version: 1
vault: acme               # slug del vault al que apuntan las rutas
k: 5                      # corte de las métricas y `limit` de cada search
queries:
  - id: acme-001          # único dentro del set
    lang: es              # es | en
    query: "¿Cómo resuelve el servicio el tenant de una petición?"
    expected:             # 1 a 3 rutas relativas a la raíz del vault, la principal primero
      - "Architecture/Tenants.md"
    answer_terms:         # opcional, 2 a 4 literales del pasaje de expected[0] que responde la pregunta
      - "TenantResolver"
      - "X-Tenant-ID"
    note: "por qué ese documento responde"   # opcional
```

`answer_terms` se copian del documento, igual que `expected`, nunca de la salida del servidor. Una lectura "cubre" la consulta cuando contiene todos los términos; así se mide si el lector obtuvo la respuesta sin depender de qué herramienta o qué chunking produjo la lectura. Es una clave opcional dentro de `version: 1`: el cargador ignora las claves desconocidas por consulta y lee esta si existe.

Las rutas van sin prefijo de vault y sin corchetes de wikilink, igual que `file_path` en las respuestas del servidor. Las notas `paraphrase:` marcan consultas que no repiten las palabras del título (caso de desajuste léxico) y `cross-lingual:` las que preguntan en un idioma por un documento escrito en el otro.

### Set de control

Cuando un ajuste (el rerank de `search`, un barrido de hiperparámetros) se elige midiendo variantes sobre un set, medirlo solo ahí reproduce el ajuste, no lo valida. Para validarlo hace falta un set aparte, congelado sin que ninguna corrida con el ajuste lo hubiera leído: otras consultas, ningún documento principal en común con el set de ajuste y, junto al archivo, su `sha256` anotado en el reporte que respalda los números. Si una etiqueta resulta mal escrita, se corrige en un set nuevo con su propio hash, nunca en el congelado.

## Cómo se corre

El comando es un script y no un subcomando de `cloudfrog-knowledge`: el entry point importa `knowledge.server`, y ese import ejecuta `config.validate()`, que exige `OPENAI_API_KEY` y `VAULT_PATH` incluso para hablar con un servidor remoto. Además, el tokenizer (`tiktoken`) y `pyyaml` son dependencias de desarrollo y no deben viajar en la distribución. Cuando el entry point pase a `knowledge.cli` con la configuración fuera del import, `cloudfrog-knowledge eval` envuelve este mismo script.

Contra un servidor local por stdio (indexa el vault en un directorio temporal; la clave se inyecta solo por entorno, nunca en un archivo ni en un argumento):

```bash
OPENAI_API_KEY="$(op item get OPENAI_API_KEY --fields api_key --reveal)" uv run python scripts/eval_retrieval.py --stdio --vault "$VAULT" --queries "$VAULT/Eval/queries.yaml" --json build/eval/report.json
```

Sin clave, con un proveedor que no la necesita (`none` o `bag-of-words`). `--index-dir` fija el directorio del índice (si no, uno temporal) y `--extra-env CLAVE=VALOR`, repetible, suma una variable al entorno del servidor; `scripts/measure_tokens.py --flow` acepta las mismas tres opciones:

```bash
uv run python scripts/eval_retrieval.py --stdio --provider none --vault "$VAULT" --queries "$VAULT/Eval/queries.yaml" --index-dir build/eval/index-none --json build/eval/none.json
```

Contra un despliegue HTTP (URL y bearer solo por entorno; el reporte no los contiene):

```bash
KNOWLEDGE_EVAL_URL=… KNOWLEDGE_EVAL_BEARER=… uv run python scripts/eval_retrieval.py --http --queries "$VAULT/Eval/queries.yaml" --json build/eval/http.json --markdown build/eval/http.md
```

Con el juez del vault (`rerank` en su `vault.yaml`), `--rerank on|off` manda ese argumento en cada `search` (sin la opción, no se manda y rige `rerank.default`). La clave del juez llega al servidor con `--pass-env NOMBRE`, que copia la variable del entorno actual por su nombre: el valor nunca aparece en la línea de comandos, donde sí quedaría con `--extra-env`. `--cover` pide en cada `search` los campos del protocolo de lectura (`file_path`, `chunk_id`, `section`, `score`) y, terminada la última búsqueda, lee la primera fila de cada consulta con `get_document(file_path, chunk_id=…, max_chars=2000)`; las lecturas van al final para que el uso que registran no mueva ningún ranking medido. `scripts/measure_tokens.py --flow` acepta `--rerank` y `--pass-env` igual:

```bash
OPENAI_API_KEY=… ACME_TYPESAFE_API_KEY=… uv run python scripts/eval_retrieval.py --stdio --vault "$VAULT" \
  --queries "$VAULT/Eval/queries.yaml" --index-dir build/eval/idx-rr-on \
  --pass-env ACME_TYPESAFE_API_KEY --rerank on --cover --json build/eval/rr-on.json
```

El reporte JSON guarda por consulta el `_meta["knowledge/rerank"]` que devolvió el servidor, cuenta las búsquedas que el juez reordenó y por qué se omitió en las demás y, con `--index-dir`, copia la sección `rerank` del `usage_stats.json` del índice (tokens facturados, USD, cubetas de latencia del juez): de ahí sale el costo por consulta.

Cada `search` registra uso en el servidor que la recibe (el boost acotado de ranking y los pares de co-recuperación). Correr el set contra un despliegue vivo mueve un poco su ranking; repetirlo muchas veces seguidas sesga sus propios números.

## Métricas

- **recall@k**: fracción de los documentos esperados que aparecen en el top k.
- **precisión@k**: documentos esperados en el top k divididos entre k. Con 1 a 3 esperados por consulta el máximo alcanzable queda muy por debajo de 1; el reporte imprime ese techo al lado.
- **MRR@k**: inverso del rango del primer esperado dentro del top k (0 si no aparece).
- **acierto@k**: fracción de consultas con al menos un esperado en el top k.
- **hit@1** y **prim@1**: fracción de consultas cuya primera fila es un esperado, y cuya primera fila es el esperado principal (`expected[0]`).
- **nDCG@k**: relevancia binaria, cada esperado del top k descontado por `log2(rango + 1)` y normalizado por el mejor orden posible de `min(esperados, k)` documentos.
- **cover@1** (con `--cover`): fracción de las consultas con `answer_terms` cuya lectura de la primera fila con `get_document(chunk_id)` (el protocolo de las `instructions`, 2.000 caracteres) contiene todos los términos.
- **tokens por consulta**: los bloques de texto de la respuesta de `search` (lo que entra al contexto del modelo), contados con tiktoken `cl100k_base`. Es un proxy: el tokenizer de Claude no es público.
