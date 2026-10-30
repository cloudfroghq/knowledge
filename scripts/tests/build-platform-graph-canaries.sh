#!/usr/bin/env bash
# build-platform-graph-canaries.sh — canary battery for scripts/build-platform-graph.sh.
#
# A gate is worth nothing until it has been seen failing. Every exit code the script
# documents (2, 3, 4, 5, 6, 7, 8) has at least one canary that produces it and at least
# one that must NOT produce it, and every expectation is an exact match on the exit
# code, never "non-zero".
#
# What the script MUST reject: missing input, a graph with no commit, a stale graph,
# two sources sharing a merge tag, a repository it cannot read, a credential shape in
# the merged output, a scan that cannot run, a dirty vendor tree, a build that comes
# back empty. What it MUST NOT touch: legitimate env-var NAME labels, identifiers that
# merely look opaque, hex digests, `${VAR}` interpolations, and the source repositories
# themselves, which stay byte-for-byte identical after a run.
#
# Every fixture is synthetic. The credential canaries are seeded with values assembled
# at runtime from the words CANARY and FAKE precisely so no real secret ever reaches
# a file.
#
# Written for the bash 3.2 that macOS ships. Run it with that interpreter:
#   /bin/bash scripts/tests/build-platform-graph-canaries.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="$(cd "$SCRIPT_DIR/.." && pwd)/build-platform-graph.sh"

SCANNER="$(cd "$SCRIPT_DIR/.." && pwd)/lib/scan-credential-shapes.py"

[ -x "$SUT" ] || { printf 'error: subject under test not executable: %s\n' "$SUT" >&2; exit 2; }
[ -f "$SCANNER" ] || { printf 'error: credential scanner not found: %s\n' "$SCANNER" >&2; exit 2; }
for tool in graphify python3 git rg; do
  command -v "$tool" > /dev/null 2>&1 || { printf 'error: required tool not found: %s\n' "$tool" >&2; exit 2; }
done

PASS_N=0
FAIL_N=0
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/platform-graph-canaries.XXXXXX")"
trap 'rm -rf "$ROOT"' EXIT

pass() { PASS_N=$((PASS_N + 1)); printf '[PASS] %s\n' "$1"; }
fail() { FAIL_N=$((FAIL_N + 1)); printf '[FAIL] %s\n' "$1"; }

# make_source <dir> <tag> <commit-mode> [extra-labels]
#   commit-mode: fresh | stale | none | null
#   extra-labels: extra node labels, several of them separated by '|'
# Builds <dir>/<tag> as a one-commit git repository holding graphify-out/graph.json.
make_source() {
  local base="$1" tag="$2" mode="$3" extra="${4:-}"
  local repo="$base/$tag"
  mkdir -p "$repo/graphify-out"
  printf '%s\n' "$tag" > "$repo/README.md"
  # graphify-out/ is gitignored in every real repository (ADR-023); the fixture mirrors
  # that so an untracked artifact does not make `git status` dirty on its own and the
  # untouched-repository canary below measures what it claims to measure.
  printf '%s\n' 'graphify-out/' > "$repo/.gitignore"
  git -C "$repo" init -q
  git -C "$repo" add -A > /dev/null
  git -C "$repo" -c user.email=canary@example.invalid -c user.name=canary \
      commit -q --no-verify -m "canary fixture" > /dev/null
  local head
  head="$(git -C "$repo" rev-parse HEAD)"
  python3 - "$repo/graphify-out/graph.json" "$tag" "$mode" "$head" "$extra" <<'PY'
import json, sys
path, tag, mode, head, extra = sys.argv[1:6]
nodes = [
    {"id": tag + "_root", "label": tag + ".php", "file_type": "code", "source_file": tag + ".php"},
    {"id": tag + "_leaf", "label": tag + "Service", "file_type": "code", "source_file": tag + ".php"},
]
for i, label in enumerate(extra.split("|") if extra else []):
    nodes.append({"id": tag + "_extra" + str(i), "label": label, "file_type": "code",
                  "source_file": ".mcp.json", "metadata": {"mcp_kind": "env_var"}})
data = {
    "directed": False,
    "multigraph": False,
    "graph": {},
    "nodes": nodes,
    "links": [{"source": tag + "_root", "target": tag + "_leaf",
               "relation": "contains", "weight": 1.0, "confidence": "EXTRACTED"}],
    "hyperedges": [],
}
if mode == "fresh":
    data["built_at_commit"] = head
elif mode == "stale":
    data["built_at_commit"] = "0" * 40
elif mode == "null":
    data["built_at_commit"] = None
# mode == "none": the key is simply absent
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
PY
}

# set_origin <repo> <url>
#   Give a fixture the remote that says which repository it is a copy of. Two
#   checkouts of one repository are exactly two directories answering the same URL
#   here, which is what the one-checkout-per-repository gate keys on. Without it a
#   fixture is a repository of its own and can never collide with another.
set_origin() {
  git -C "$1" remote add origin "$2"
}

# make_surface <repo> <name>
#   Add <repo>/<name>/graphify-out/graph.json to an EXISTING fixture repository,
#   stamped with that repository's HEAD. Models a subpath target such as
#   `server:lib`: several surfaces read out of one checkout, which is the
#   shape the gate must keep accepting.
make_surface() {
  local repo="$1" name="$2"
  mkdir -p "$repo/$name/graphify-out"
  python3 - "$repo/$name/graphify-out/graph.json" "$name" "$(git -C "$repo" rev-parse HEAD)" <<'PY'
import json, sys
path, name, head = sys.argv[1:4]
data = {
    "directed": False,
    "multigraph": False,
    "graph": {},
    "nodes": [
        {"id": name + "_root", "label": name + ".php", "file_type": "code",
         "source_file": name + ".php"},
        {"id": name + "_leaf", "label": name + "Service", "file_type": "code",
         "source_file": name + ".php"},
    ],
    "links": [{"source": name + "_root", "target": name + "_leaf",
               "relation": "contains", "weight": 1.0, "confidence": "EXTRACTED"}],
    "hyperedges": [],
    "built_at_commit": head,
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
PY
}

# run_sut <workdir-name> <expected-exit> <args...>
# Leaves stdout+stderr in $LAST_LOG and the output directory in $LAST_OUT.
run_sut() {
  local name="$1" expected="$2"; shift 2
  LAST_OUT="$ROOT/$name/out"
  LAST_LOG="$ROOT/$name/log.txt"
  mkdir -p "$LAST_OUT" "$ROOT/$name"
  local rc=0
  /bin/bash "$SUT" --out "$LAST_OUT" "$@" > "$LAST_LOG" 2>&1 || rc=$?
  LAST_RC="$rc"
  [ "$rc" -eq "$expected" ]
}

# inventory <dir> — sorted "relative-path sha256" listing, used to prove a repo is untouched.
inventory() {
  python3 - "$1" <<'PY'
import hashlib, os, sys
root = sys.argv[1]
rows = []
for dirpath, dirnames, filenames in os.walk(root):
    dirnames.sort()
    for name in sorted(filenames):
        full = os.path.join(dirpath, name)
        if not os.path.isfile(full) or os.path.islink(full):
            rows.append(os.path.relpath(full, root) + " <special>")
            continue
        with open(full, "rb") as fh:
            rows.append(os.path.relpath(full, root) + " " + hashlib.sha256(fh.read()).hexdigest())
print("\n".join(sorted(rows)))
PY
}

prov_get() {
  python3 - "$1" "$2" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
node = data
for part in sys.argv[2].split("."):
    node = node[int(part)] if isinstance(node, list) else node[part]
print(json.dumps(node))
PY
}

printf 'canary battery for %s\n\n' "$SUT"

# --------------------------------------------------------------------------- #
# Group A — what the script MUST reject
# --------------------------------------------------------------------------- #
# Synthetic canary values, assembled at runtime from the words CANARY and FAKE so no
# real credential ever reaches a fixture file. OPAQUE_CANARY is 40 characters and trips
# the entropy heuristic; SHORT_CANARY is 20 and only trips the rules that read context.
OPAQUE_CANARY="$(printf '%s' 'C4n4Ry' 'F4k3' 'C4n4Ry' 'F4k3' 'C4n4Ry' 'F4k3' 'C4n4Ry' 'F4k3')"
SHORT_CANARY="$(printf '%s' 'C4n4Ry' 'F4k3' 'C4n4Ry' 'F4k3')"

printf '== A. Lo que el gate DEBE detectar ==\n'

# A1 — unknown argument
if run_sut a1 2 --definitely-not-a-flag; then
  if rg -q "unknown argument" "$LAST_LOG"; then
    pass "A1 argumento desconocido: exit 2 y mensaje propio"
  else
    fail "A1 exit 2 pero sin el mensaje esperado"
  fi
else
  fail "A1 esperaba exit 2, obtuvo $LAST_RC"
fi

# A2 — a single source cannot be merged
A2="$ROOT/fixtures/a2"; make_source "$A2" solo fresh
if run_sut a2 2 --source "$A2/solo/graphify-out/graph.json"; then
  if rg -q "at least two source graphs" "$LAST_LOG"; then
    pass "A2 un solo insumo: exit 2, no una expansión de array vacío"
  else
    fail "A2 exit 2 pero sin el mensaje esperado"
  fi
else
  fail "A2 esperaba exit 2, obtuvo $LAST_RC"
fi
if rg -q "unbound variable" "$LAST_LOG"; then
  fail "A2 el script murió con 'unbound variable' bajo bash 3.2"
else
  pass "A2 sin 'unbound variable' bajo bash 3.2"
fi

# A3 — missing input file
A3="$ROOT/fixtures/a3"; make_source "$A3" alpha fresh
if run_sut a3 2 \
    --source "$A3/alpha/graphify-out/graph.json" \
    --source "$A3/no-existe/graphify-out/graph.json"; then
  if rg -q "source graph not found" "$LAST_LOG"; then
    pass "A3 insumo inexistente: exit 2"
  else
    fail "A3 exit 2 pero sin el mensaje esperado"
  fi
else
  fail "A3 esperaba exit 2, obtuvo $LAST_RC"
fi

# A4 — built_at_commit key absent (ADR-026)
A4="$ROOT/fixtures/a4"; make_source "$A4" alpha fresh; make_source "$A4" beta none
if run_sut a4 3 \
    --source "$A4/alpha/graphify-out/graph.json" \
    --source "$A4/beta/graphify-out/graph.json"; then
  if rg -q "no built_at_commit" "$LAST_LOG"; then
    pass "A4 grafo sin built_at_commit: exit 3 (ADR-026)"
  else
    fail "A4 exit 3 pero sin el mensaje esperado"
  fi
else
  fail "A4 esperaba exit 3, obtuvo $LAST_RC"
fi

# A5 — built_at_commit explicitly null
A5="$ROOT/fixtures/a5"; make_source "$A5" alpha fresh; make_source "$A5" beta null
if run_sut a5 3 \
    --source "$A5/alpha/graphify-out/graph.json" \
    --source "$A5/beta/graphify-out/graph.json"; then
  pass "A5 built_at_commit null: exit 3"
else
  fail "A5 esperaba exit 3, obtuvo $LAST_RC"
fi

# A6 — stale graph against repo HEAD
A6="$ROOT/fixtures/a6"; make_source "$A6" alpha fresh; make_source "$A6" beta stale
if run_sut a6 4 \
    --source "$A6/alpha/graphify-out/graph.json" \
    --source "$A6/beta/graphify-out/graph.json"; then
  if rg -q "stale source graph" "$LAST_LOG"; then
    pass "A6 grafo obsoleto frente a HEAD: exit 4"
  else
    fail "A6 exit 4 pero sin el mensaje esperado"
  fi
else
  fail "A6 esperaba exit 4, obtuvo $LAST_RC"
fi

# A7 — two sources resolving to the same merge tag would be fused by nx.compose
A7="$ROOT/fixtures/a7"
make_source "$A7/one" alpha fresh
make_source "$A7/two" alpha fresh
if run_sut a7 2 \
    --source "$A7/one/alpha/graphify-out/graph.json" \
    --source "$A7/two/alpha/graphify-out/graph.json"; then
  if rg -q "same merge tag" "$LAST_LOG"; then
    pass "A7 dos insumos con el mismo tag: exit 2"
  else
    fail "A7 exit 2 pero sin el mensaje esperado"
  fi
else
  fail "A7 esperaba exit 2, obtuvo $LAST_RC"
fi

# A8 — credential shape in the merged output blocks publication
CANARY_SECRET="AKIA$(printf '%s' 'CANARY' 'FAKE')000000"
A8="$ROOT/fixtures/a8"
make_source "$A8" alpha fresh
make_source "$A8" beta fresh "$CANARY_SECRET"
if run_sut a8 5 \
    --source "$A8/alpha/graphify-out/graph.json" \
    --source "$A8/beta/graphify-out/graph.json"; then
  if rg -q "credential scan" "$LAST_LOG"; then
    pass "A8 credencial en el grafo fusionado: exit 5"
  else
    fail "A8 exit 5 pero sin el mensaje esperado"
  fi
  if [ -f "$LAST_OUT/merged-graph.json" ]; then
    fail "A8 el grafo se publicó pese a la credencial"
  else
    pass "A8 no se publicó merged-graph.json"
  fi
  if [ -f "$LAST_OUT/.merged-graph.tmp.json" ]; then
    fail "A8 quedó el temporal con la credencial en disco"
  else
    pass "A8 el temporal se borró"
  fi
  if rg -qF "$CANARY_SECRET" "$LAST_LOG"; then
    fail "A8 el gate imprimió el valor de la credencial"
  else
    pass "A8 el gate no imprimió el valor, solo patrón y línea"
  fi
else
  fail "A8 esperaba exit 5, obtuvo $LAST_RC"
fi

# A9 — the shape of an `authpt` query parameter: no vendor prefix, no keyword a
# wordlist would carry. This is the case that used to publish with exit 0.
A9="$ROOT/fixtures/a9"
make_source "$A9" alpha fresh
make_source "$A9" beta fresh "authpt=$OPAQUE_CANARY"
if run_sut a9 5 \
    --source "$A9/alpha/graphify-out/graph.json" \
    --source "$A9/beta/graphify-out/graph.json"; then
  if rg -q "legacy_auth_param" "$LAST_LOG"; then
    pass "A9 'authpt=<valor>' sin prefijo: exit 5 por legacy_auth_param"
  else
    fail "A9 exit 5 pero lo detectó otra regla: $(rg -N -o '[a-z_]+ at line' "$LAST_LOG" | head -1)"
  fi
  if [ -f "$LAST_OUT/merged-graph.json" ]; then
    fail "A9 el grafo se publicó pese a la credencial"
  else
    pass "A9 no se publicó merged-graph.json"
  fi
  if rg -qF "$OPAQUE_CANARY" "$LAST_LOG"; then
    fail "A9 el gate imprimió el valor"
  else
    pass "A9 el gate no imprimió el valor"
  fi
else
  fail "A9 esperaba exit 5, obtuvo $LAST_RC"
fi

# A10 — an opaque value with no context at all, as a bare node label. Nothing but the
# entropy heuristic can see this one.
A10="$ROOT/fixtures/a10"
make_source "$A10" alpha fresh
make_source "$A10" beta fresh "$OPAQUE_CANARY"
if run_sut a10 5 \
    --source "$A10/alpha/graphify-out/graph.json" \
    --source "$A10/beta/graphify-out/graph.json"; then
  if rg -q "opaque_high_entropy_value" "$LAST_LOG"; then
    pass "A10 valor opaco sin contexto como label: exit 5 por opaque_high_entropy_value"
  else
    fail "A10 exit 5 pero lo detectó otra regla"
  fi
  if [ -f "$LAST_OUT/merged-graph.json" ]; then
    fail "A10 el grafo se publicó pese al valor opaco"
  else
    pass "A10 no se publicó merged-graph.json"
  fi
else
  fail "A10 esperaba exit 5, obtuvo $LAST_RC"
fi

# A11 — an assignment WITHOUT quotes around the value. Requiring the quotes is what
# made the generic rule miss this shape.
A11="$ROOT/fixtures/a11"
make_source "$A11" alpha fresh
make_source "$A11" beta fresh "password=$SHORT_CANARY"
if run_sut a11 5 \
    --source "$A11/alpha/graphify-out/graph.json" \
    --source "$A11/beta/graphify-out/graph.json"; then
  if rg -q "assigned_secret_literal" "$LAST_LOG"; then
    pass "A11 'password=<valor>' sin comillas: exit 5 por assigned_secret_literal"
  else
    fail "A11 exit 5 pero lo detectó otra regla"
  fi
else
  fail "A11 esperaba exit 5, obtuvo $LAST_RC"
fi

# A12 — a URL query parameter with an opaque value and a name nobody put in a wordlist.
A12="$ROOT/fixtures/a12"
make_source "$A12" alpha fresh
make_source "$A12" beta fresh "https://mf.example.invalid/checkemail?ticket=$SHORT_CANARY"
if run_sut a12 5 \
    --source "$A12/alpha/graphify-out/graph.json" \
    --source "$A12/beta/graphify-out/graph.json"; then
  if rg -q "url_query_opaque_value" "$LAST_LOG"; then
    pass "A12 parámetro de URL con valor opaco: exit 5 por url_query_opaque_value"
  else
    fail "A12 exit 5 pero lo detectó otra regla"
  fi
else
  fail "A12 esperaba exit 5, obtuvo $LAST_RC"
fi

# A13 — "no pude mirar" is not "no encontré nada". A scanner that will not run must
# stop the publication, not wave it through. The fault is injected on a COPY of the
# script tree, so nothing in the real one is a test hook.
A13="$ROOT/fixtures/a13"
make_source "$A13" alpha fresh
make_source "$A13" beta fresh
COPY="$ROOT/a13-copy/scripts"
mkdir -p "$COPY/lib"
cp "$SUT" "$COPY/build-platform-graph.sh"
printf '%s\n' '#!/usr/bin/env python3' 'import sys' 'sys.exit(3)' > "$COPY/lib/scan-credential-shapes.py"
A13_OUT="$ROOT/a13/out"; A13_LOG="$ROOT/a13/log.txt"
mkdir -p "$A13_OUT" "$ROOT/a13"
a13_rc=0
/bin/bash "$COPY/build-platform-graph.sh" --out "$A13_OUT" \
    --source "$A13/alpha/graphify-out/graph.json" \
    --source "$A13/beta/graphify-out/graph.json" > "$A13_LOG" 2>&1 || a13_rc=$?
if [ "$a13_rc" -eq 5 ]; then
  if rg -q "could not run" "$A13_LOG"; then
    pass "A13 escáner que no corre: exit 5 y lo dice ('could not run'), no PASS"
  else
    fail "A13 exit 5 pero sin distinguir 'no pude mirar' de 'no encontré nada'"
  fi
  if [ -f "$A13_OUT/merged-graph.json" ]; then
    fail "A13 publicó con el escáner caído"
  else
    pass "A13 no publicó nada con el escáner caído"
  fi
else
  fail "A13 esperaba exit 5, obtuvo $a13_rc"
fi

# A14 — the scanner missing outright is caught before anything is built.
A14_COPY="$ROOT/a14-copy/scripts"
mkdir -p "$A14_COPY"
cp "$SUT" "$A14_COPY/build-platform-graph.sh"
A14_LOG="$ROOT/a14/log.txt"; mkdir -p "$ROOT/a14"
a14_rc=0
/bin/bash "$A14_COPY/build-platform-graph.sh" --out "$ROOT/a14/out" \
    --source "$A13/alpha/graphify-out/graph.json" \
    --source "$A13/beta/graphify-out/graph.json" > "$A14_LOG" 2>&1 || a14_rc=$?
if [ "$a14_rc" -eq 2 ] && rg -q "credential scanner not found" "$A14_LOG"; then
  pass "A14 escáner ausente: exit 2 antes de construir nada"
else
  fail "A14 esperaba exit 2 con 'credential scanner not found', obtuvo $a14_rc"
fi

# A15 — a declared repository that git cannot read is neither fresh nor stale: it is a
# staleness gate that could not run, and it must stop the run instead of recording null.
A15="$ROOT/fixtures/a15"
make_source "$A15" alpha fresh
make_source "$A15" beta fresh
mkdir -p "$A15/broken/.git"
if run_sut a15 2 \
    --source "$A15/alpha/graphify-out/graph.json" \
    --source "$A15/beta/graphify-out/graph.json::$A15/broken"; then
  if rg -q "cannot read HEAD" "$LAST_LOG"; then
    pass "A15 repositorio ilegible: exit 2, el gate de frescura no se salta en silencio"
  else
    fail "A15 exit 2 pero sin el mensaje esperado"
  fi
  if [ -f "$LAST_OUT/merged-graph.json" ]; then
    fail "A15 publicó sin poder comprobar la frescura"
  else
    pass "A15 no publicó sin poder comprobar la frescura"
  fi
else
  fail "A15 esperaba exit 2, obtuvo $LAST_RC"
fi

# A16 — two checkouts of the same repository, sitting on different commits. Each one
# is fresh against its OWN HEAD, so the staleness gate waves both through and the
# artifact would fuse two trees into a state that exists in neither.
SHARED_ORIGIN="git@example.invalid:canary/shared.git"
A16="$ROOT/fixtures/a16"
make_source "$A16" slot_a fresh
make_source "$A16" slot_b fresh
set_origin "$A16/slot_a" "$SHARED_ORIGIN"
set_origin "$A16/slot_b" "$SHARED_ORIGIN"
if run_sut a16 8 \
    --source "$A16/slot_a/graphify-out/graph.json" \
    --source "$A16/slot_b/graphify-out/graph.json"; then
  if rg -q "entered through 2 checkouts" "$LAST_LOG"; then
    pass "A16 dos checkouts del mismo repositorio en commits distintos: exit 8, nombrando los dos"
  else
    fail "A16 exit 8 pero sin nombrar los checkouts en conflicto"
  fi
  if [ -f "$LAST_OUT/merged-graph.json" ]; then
    fail "A16 publicó un artefacto que mezcla dos árboles"
  else
    pass "A16 no publicó nada"
  fi
else
  fail "A16 esperaba exit 8, obtuvo $LAST_RC"
fi

# A17 — the same two checkouts sitting on the SAME commit. This is the real shape on
# the machine today, and it is the case that makes a commit comparison useless as a
# gate: nothing differs until one of the two takes a commit, and by then the artifact
# is already wrong. The gate is on the pair of checkouts, so it fires here too.
A17="$ROOT/fixtures/a17"
make_source "$A17" twin_a fresh
set_origin "$A17/twin_a" "$SHARED_ORIGIN"
cp -R "$A17/twin_a" "$A17/twin_b"
if [ "$(git -C "$A17/twin_a" rev-parse HEAD)" != "$(git -C "$A17/twin_b" rev-parse HEAD)" ]; then
  fail "A17 la fixture no reproduce el caso: los dos checkouts no están en el mismo commit"
fi
if run_sut a17 8 \
    --source "$A17/twin_a/graphify-out/graph.json" \
    --source "$A17/twin_b/graphify-out/graph.json"; then
  pass "A17 dos checkouts del mismo repositorio en el MISMO commit: exit 8 igualmente"
else
  fail "A17 esperaba exit 8 con commits iguales, obtuvo $LAST_RC"
fi

# --------------------------------------------------------------------------- #
# Group B — what the script MUST NOT touch or reject
# --------------------------------------------------------------------------- #
printf '\n== B. Lo que el gate NO debe tocar ==\n'

# B1 — happy path over two synthetic repositories
B1="$ROOT/fixtures/b1"
make_source "$B1" alpha fresh
make_source "$B1" beta fresh
BEFORE_ALPHA="$(inventory "$B1/alpha")"
BEFORE_BETA="$(inventory "$B1/beta")"
if run_sut b1 0 \
    --source "$B1/alpha/graphify-out/graph.json" \
    --source "$B1/beta/graphify-out/graph.json"; then
  pass "B1 camino feliz: exit 0"
  ok=1
  for artifact in merged-graph.json merged-graph.provenance.json merged-graph.report.md; do
    [ -s "$LAST_OUT/$artifact" ] || { fail "B1 falta el artefacto $artifact"; ok=0; }
  done
  [ "$ok" -eq 1 ] && pass "B1 los tres artefactos existen y no están vacíos"
  P="$LAST_OUT/merged-graph.provenance.json"
  if [ "$(prov_get "$P" merged.cross_repo_links)" = "0" ]; then
    pass "B1 aristas entre repositorios: 0 (medido, no supuesto)"
  else
    fail "B1 cross_repo_links inesperado: $(prov_get "$P" merged.cross_repo_links)"
  fi
  if [ "$(prov_get "$P" merged.built_at_commit_present)" = "false" ]; then
    pass "B1 el archivo fusionado no trae built_at_commit; el sidecar lo preserva"
  else
    fail "B1 built_at_commit_present inesperado"
  fi
  a_commit="$(prov_get "$P" sources.0.built_at_commit | tr -d '\"')"
  a_head="$(git -C "$B1/alpha" rev-parse HEAD)"
  if [ "$a_commit" = "$a_head" ]; then
    pass "B1 el sidecar preserva el built_at_commit de cada insumo"
  else
    fail "B1 el sidecar guardó $a_commit y el repo está en $a_head"
  fi
  if [ "$(prov_get "$P" sources.0.fresh)" = "true" ]; then
    pass "B1 el insumo se marca al día"
  else
    fail "B1 fresh inesperado"
  fi
else
  fail "B1 esperaba exit 0, obtuvo $LAST_RC"
fi

# B2 — the source repositories are byte-for-byte identical after the run
if [ "$(inventory "$B1/alpha")" = "$BEFORE_ALPHA" ] && [ "$(inventory "$B1/beta")" = "$BEFORE_BETA" ]; then
  pass "B2 los repositorios de origen quedaron intactos (hash a hash)"
else
  fail "B2 el script escribió dentro de un repositorio de origen"
fi
if [ -z "$(git -C "$B1/alpha" status --porcelain)" ] && [ -z "$(git -C "$B1/beta" status --porcelain)" ]; then
  pass "B2 'git status' limpio en los dos repositorios de origen"
else
  fail "B2 'git status' sucio en algún repositorio de origen"
fi

# B3 — env-var NAMES are not credentials: the scanner must not trip on them
B3="$ROOT/fixtures/b3"
make_source "$B3" alpha fresh "JIRA_API_TOKEN"
make_source "$B3" beta fresh "CONFLUENCE_API_TOKEN"
if run_sut b3 0 \
    --source "$B3/alpha/graphify-out/graph.json" \
    --source "$B3/beta/graphify-out/graph.json"; then
  pass "B3 nombres de variable (JIRA_API_TOKEN, CONFLUENCE_API_TOKEN) no disparan el escáner"
else
  fail "B3 falso positivo del escáner: exit $LAST_RC"
fi

# B4 — --allow-stale downgrades the staleness gate instead of removing it
B4="$ROOT/fixtures/b4"
make_source "$B4" alpha fresh
make_source "$B4" beta stale
if run_sut b4 0 --allow-stale \
    --source "$B4/alpha/graphify-out/graph.json" \
    --source "$B4/beta/graphify-out/graph.json"; then
  if rg -q "warning: stale source" "$LAST_LOG"; then
    pass "B4 --allow-stale avisa y sigue"
  else
    fail "B4 --allow-stale siguió pero sin avisar"
  fi
  if [ "$(prov_get "$LAST_OUT/merged-graph.provenance.json" sources.1.fresh)" = "false" ]; then
    pass "B4 el sidecar registra el insumo obsoleto como fresh:false"
  else
    fail "B4 el sidecar no registró la obsolescencia"
  fi
else
  fail "B4 esperaba exit 0 con --allow-stale, obtuvo $LAST_RC"
fi

# B5 — the GRAPH::REPO form uses the declared repository for the freshness check
B5="$ROOT/fixtures/b5"
make_source "$B5" alpha fresh
make_source "$B5" beta fresh
mkdir -p "$B5/detached/graphify-out"
python3 - "$B5/beta/graphify-out/graph.json" "$B5/detached/graphify-out/graph.json" <<'PY'
import json, shutil, sys
shutil.copyfile(sys.argv[1], sys.argv[2])
PY
if run_sut b5 0 \
    --source "$B5/alpha/graphify-out/graph.json" \
    --source "$B5/detached/graphify-out/graph.json::$B5/beta"; then
  if [ "$(prov_get "$LAST_OUT/merged-graph.provenance.json" sources.1.fresh)" = "true" ]; then
    pass "B5 la forma GRAPH::REPO resuelve la frescura contra el repositorio declarado"
  else
    fail "B5 la forma GRAPH::REPO no usó el repositorio declarado"
  fi
else
  fail "B5 esperaba exit 0, obtuvo $LAST_RC"
fi

# B6 — a dirty working tree warns but does not block: the commit gate compares
# commits, and the merge stays reproducible from the commits the sidecar records.
B6D="$ROOT/fixtures/b6d"
make_source "$B6D" alpha fresh
make_source "$B6D" beta fresh
printf 'uncommitted\n' > "$B6D/beta/scratch.txt"
if run_sut b6d 0 \
    --source "$B6D/alpha/graphify-out/graph.json" \
    --source "$B6D/beta/graphify-out/graph.json"; then
  if rg -q "uncommitted change" "$LAST_LOG"; then
    pass "B6 árbol de trabajo sucio: avisa y sigue (exit 0)"
  else
    fail "B6 árbol sucio sin aviso"
  fi
  if [ "$(prov_get "$LAST_OUT/merged-graph.provenance.json" sources.1.uncommitted_files)" = "1" ]; then
    pass "B6 el sidecar registra los archivos sin commitear"
  else
    fail "B6 el sidecar no registró uncommitted_files"
  fi
  if [ "$(prov_get "$LAST_OUT/merged-graph.provenance.json" sources.0.uncommitted_files)" = "0" ]; then
    pass "B6 el insumo limpio se registra con 0 archivos sin commitear"
  else
    fail "B6 uncommitted_files inesperado en el insumo limpio"
  fi
else
  fail "B6 esperaba exit 0 con árbol sucio, obtuvo $LAST_RC"
fi

# B7 — --help is not an error
if run_sut b7 0 --help; then
  if rg -q "build the platform code graph" "$LAST_LOG"; then
    pass "B7 --help imprime la ayuda y termina en 0"
  else
    fail "B7 --help terminó en 0 pero no imprimió la ayuda"
  fi
else
  fail "B7 --help esperaba exit 0, obtuvo $LAST_RC"
fi

# B8 — a source with no git checkout at all is a DECLARED absence, not a failed read:
# the run continues, says so out loud, and the sidecar records fresh:null so nobody
# reads it as a source whose staleness was checked.
B8="$ROOT/fixtures/b8"
make_source "$B8" alpha fresh
mkdir -p "$B8/norepo/graphify-out"
python3 - "$B8/alpha/graphify-out/graph.json" "$B8/norepo/graphify-out/graph.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
data["built_at_commit"] = "1" * 40
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
PY
if run_sut b8 0 \
    --source "$B8/alpha/graphify-out/graph.json" \
    --source "$B8/norepo/graphify-out/graph.json"; then
  if rg -q "staleness was NOT checked" "$LAST_LOG"; then
    pass "B8 insumo sin repositorio: avisa que la frescura NO se comprobó"
  else
    fail "B8 insumo sin repositorio aceptado en silencio"
  fi
  if [ "$(prov_get "$LAST_OUT/merged-graph.provenance.json" sources.1.fresh)" = "null" ]; then
    pass "B8 el sidecar lo registra como fresh:null, no como al día"
  else
    fail "B8 el sidecar no registró fresh:null"
  fi
else
  fail "B8 esperaba exit 0, obtuvo $LAST_RC"
fi

# B9 — shapes that LOOK like the new rules and must not trip them: a long camelCase
# identifier, hex digests with no upper case, an interpolation, and a numeric count.
B9="$ROOT/fixtures/b9"
make_source "$B9" alpha fresh "isOutgoingServer2serverGroupShareEnabled|d41d8cd98f00b204e9800998ecf8427e"
make_source "$B9" beta fresh "api_key: \${JIRA_API_TOKEN}|token_count = 42|da39a3ee5e6b4b0d3255bfef95601890afd80709"
if run_sut b9 0 \
    --source "$B9/alpha/graphify-out/graph.json" \
    --source "$B9/beta/graphify-out/graph.json"; then
  pass "B9 identificador largo, digests hex, interpolación y conteo: exit 0, sin falso positivo"
else
  fail "B9 falso positivo del escáner: exit $LAST_RC"
fi

# B10 — several surfaces read out of ONE checkout is the shape the platform graph is
# built on (server:lib, :core, :ocs, :3rdparty/sabre, :apps). One repository, one
# checkout, many subpaths: the gate must not confuse it with the split it rejects.
B10="$ROOT/fixtures/b10"
make_source "$B10" whole fresh
set_origin "$B10/whole" "git@example.invalid:canary/whole.git"
make_surface "$B10/whole" surface_one
make_surface "$B10/whole" surface_two
if run_sut b10 0 \
    --source "$B10/whole/surface_one/graphify-out/graph.json" \
    --source "$B10/whole/surface_two/graphify-out/graph.json"; then
  pass "B10 dos superficies del mismo checkout: exit 0, el gate no las confunde con dos copias"
  P10="$LAST_OUT/merged-graph.provenance.json"
  r0="$(prov_get "$P10" sources.0.repo_root | tr -d '\"')"
  r1="$(prov_get "$P10" sources.1.repo_root | tr -d '\"')"
  if [ -n "$r0" ] && [ "$r0" = "$r1" ]; then
    pass "B10 el sidecar resuelve las dos superficies al mismo checkout"
  else
    fail "B10 repo_root distinto entre superficies del mismo checkout: '$r0' vs '$r1'"
  fi
else
  fail "B10 esperaba exit 0 con dos superficies de un checkout, obtuvo $LAST_RC"
fi

# B11 — two checkouts side by side with DIFFERENT remotes are two repositories, not two
# copies. Guards against a gate that keys on the parent directory instead of on the
# repository, and fixes the two provenance fields the gate reads.
B11="$ROOT/fixtures/b11"
make_source "$B11" alpha fresh
make_source "$B11" beta fresh
set_origin "$B11/alpha" "git@example.invalid:canary/alpha.git"
set_origin "$B11/beta" "git@example.invalid:canary/beta.git"
if run_sut b11 0 \
    --source "$B11/alpha/graphify-out/graph.json" \
    --source "$B11/beta/graphify-out/graph.json"; then
  pass "B11 dos repositorios distintos lado a lado: exit 0"
  P11="$LAST_OUT/merged-graph.provenance.json"
  if [ "$(prov_get "$P11" sources.0.repo_identity | tr -d '\"')" = "git@example.invalid:canary/alpha" ]; then
    pass "B11 el sidecar guarda la identidad del repositorio con el sufijo .git normalizado"
  else
    fail "B11 repo_identity inesperado: $(prov_get "$P11" sources.0.repo_identity)"
  fi
  if [ "$(prov_get "$P11" sources.0.repo_branch)" != "null" ]; then
    pass "B11 el sidecar registra la rama de cada insumo"
  else
    fail "B11 el sidecar no registró la rama"
  fi
  if [ "$(prov_get "$P11" sibling_checkouts)" = "[]" ]; then
    pass "B11 una corrida con --source no inventa copias hermanas: el registro no se consulta"
  else
    fail "B11 sibling_checkouts debería estar vacío en una corrida con --source"
  fi
else
  fail "B11 esperaba exit 0 con dos repositorios distintos, obtuvo $LAST_RC"
fi

# --------------------------------------------------------------------------- #
# Group C — the default source set: graph.targets in the default vault's
# Contexts/config.json (ADR-039, ADR-047), read through the repository registry
# (knowledge.graph_targets.graph_targets_detail(), via `uv run` from the knowledge
# checkout).
#
# These cases own their declaration. This server is project-agnostic and no vault
# lives in this repository, so a battery that keyed on a real vault would be
# asserting on whatever the machine running it happens to declare -- green on one
# machine and red on the next, for a reason that says nothing about the gate. Each
# case below therefore writes its own vault with its own registry and graph.targets,
# exactly the way tests/test_repo_registry.py builds a declaration from scratch.
#
# The seam is PYTHONPATH. knowledge.config derives the project root from the
# package's own resolved location, so a copy of src/ under a synthetic root,
# placed first on PYTHONPATH, makes that root the project root: no `.env` of the
# developer's checkout can reach the run. The synthetic vault is the default vault
# through VAULT_PATH. The copy is taken from the working tree at run time, so it
# is never a stale duplicate of the source it stands in for. A symlink does not
# work here: .resolve() follows it back to the real checkout. C0 proves the seam
# before any gate is exercised through it.
#
# `env -i` on every invocation means nothing ambient (an operator's own CD_*
# exports, say) can leak in, and repos.local.json on the machine running this
# battery is never consulted, because an environment override always wins over
# it and every root these cases declare is pointed explicitly. XDG_CONFIG_HOME
# points at an empty directory, so no vault initialized on this machine joins
# the synthetic one.
# --------------------------------------------------------------------------- #
printf '\n== C. Conjunto de insumos por omisión (graph.targets) ==\n'

CHECKOUT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
GT="$ROOT/fixtures/graphtargets"
GT_ROOT="$GT/knowledge-root"
GT_VAULT="$GT/vault"
GT_CHECKOUTS="$GT/checkouts"
mkdir -p "$GT" "$GT_CHECKOUTS" "$GT_ROOT" "$GT_VAULT/Contexts"

cp -R "$CHECKOUT_ROOT/src" "$GT_ROOT/src" \
  || { printf 'error: could not stage the synthetic project root at %s\n' "$GT_ROOT" >&2; exit 2; }

# Five repositories, one root that resolves and one that nobody declares, so the
# three skip reasons the script distinguishes each have a declared target that
# produces it and cannot be confused with another:
#   alpha, beta  -- fresh graphs, the two sources that get fused.
#   gamma        -- a checkout on disk with no graph built yet (missing_graph).
#   delta        -- a checkout whose graph is stale against its HEAD (stale_skip).
#   epsilon      -- declared under a root this machine never points at, twice, so
#                   the several-surfaces-of-one-repository shape is covered too
#                   (unresolved).
# 6 declared, 2 fused, 4 skipped (1 missing_graph + 1 stale_skip + 2 unresolved)
# is the exact shape this produces.
cat > "$GT_VAULT/Contexts/config.json" <<'JSON'
{
  "repos": {
    "roots": [
      {"id": "checkouts", "env": "CD_CHECKOUTS_ROOT"},
      {"id": "absent", "env": "CD_ABSENT_ROOT"}
    ],
    "groups": {
      "standalone": "An independent clone. Nothing else brings it down."
    },
    "entries": [
      {"id": "alpha", "remote": "alpha", "group": "standalone", "root": "checkouts", "path": "alpha"},
      {"id": "beta", "remote": "beta", "group": "standalone", "root": "checkouts", "path": "beta"},
      {"id": "gamma", "remote": "gamma", "group": "standalone", "root": "checkouts", "path": "gamma"},
      {"id": "delta", "remote": "delta", "group": "standalone", "root": "checkouts", "path": "delta"},
      {"id": "epsilon", "remote": "epsilon", "group": "standalone", "root": "absent", "path": "epsilon"}
    ]
  },
  "graph": {
    "targets": [
      {"repo": "alpha", "path": "."},
      {"repo": "beta", "path": "."},
      {"repo": "gamma", "path": "."},
      {"repo": "delta", "path": "."},
      {"repo": "epsilon", "path": "."},
      {"repo": "epsilon", "path": "lib"}
    ]
  }
}
JSON

# make_bare_repo <dir> — a one-commit git repository with no graphify-out at all:
# the "no graph built yet" case discovery has to skip, never fail on.
make_bare_repo() {
  local repo="$1"
  mkdir -p "$repo"
  printf 'x\n' > "$repo/README.md"
  git -C "$repo" init -q
  git -C "$repo" add -A > /dev/null
  git -C "$repo" -c user.email=canary@example.invalid -c user.name=canary \
      commit -q --no-verify -m "canary fixture" > /dev/null
}

make_source "$GT_CHECKOUTS" alpha fresh
make_source "$GT_CHECKOUTS" beta fresh
make_bare_repo "$GT_CHECKOUTS/gamma"
make_source "$GT_CHECKOUTS" delta stale

# run_gt <name> <expected-exit> [extra SUT args...] — invokes the real script with a
# wiped environment, the synthetic declaration above, and whatever extra CLI flags
# the caller passes (e.g. --allow-stale).
run_gt() {
  local name="$1" expected="$2"; shift 2
  LAST_OUT="$ROOT/$name/out"
  LAST_LOG="$ROOT/$name/log.txt"
  mkdir -p "$LAST_OUT" "$ROOT/$name"
  local rc=0
  env -i PATH="$PATH" HOME="$HOME" XDG_CONFIG_HOME="$GT/xdg-config" \
      PYTHONPATH="$GT_ROOT/src" VAULT_PATH="$GT_VAULT" \
      CD_CHECKOUTS_ROOT="$GT_CHECKOUTS" \
      CD_ABSENT_ROOT="$GT/no-such-absent-root" \
      /bin/bash "$SUT" --out "$LAST_OUT" "$@" > "$LAST_LOG" 2>&1 || rc=$?
  LAST_RC="$rc"
  [ "$rc" -eq "$expected" ]
}

# skip_stage_counts <provenance.json> — prints "missing_graph stale_skip unresolved"
# so assertions do not depend on the order skipped_targets happens to list them in.
skip_stage_counts() {
  python3 -c '
import collections, json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    d = json.load(fh)
c = collections.Counter(s["stage"] for s in d["skipped_targets"])
print(c.get("missing_graph", 0), c.get("stale_skip", 0), c.get("unresolved", 0))
' "$1"
}

# C0 — the seam, before anything is measured through it. A PYTHONPATH or a
# VAULT_PATH that stopped redirecting the declaration would leave every case below
# reading another registry and failing for a reason that has nothing to do with the gate.
C0_LOG="$ROOT/c0-seam.txt"
( cd "$CHECKOUT_ROOT" && env PYTHONPATH="$GT_ROOT/src" VAULT_PATH="$GT_VAULT" XDG_CONFIG_HOME="$GT/xdg-config" \
  uv run python3 -c \
  'from knowledge import graph_targets
registry, error = graph_targets.registry_state()
print(registry.declaration_path if registry is not None else error)
print(len(graph_targets.graph_targets_detail()))' ) > "$C0_LOG" 2>&1
# Compared by its tail: the registry normalizes the path it reports, so a symlinked
# or doubled-up temporary directory ($TMPDIR already ends in a slash on macOS) would
# never match the path as it was assembled here. The tail names the synthetic vault.
if rg -qF "/graphtargets/vault/Contexts/config.json" "$C0_LOG" && rg -qx '6' "$C0_LOG"; then
  pass "C0 la declaración que lee el registro es la del vault sintético: 6 objetivos declarados"
else
  fail "C0 el seam PYTHONPATH/VAULT_PATH no redirige la declaración; salida: $(rg -N '^' "$C0_LOG" | tr '\n' ' ')"
fi

# C1 — the happy path: two fused, the other four declared targets skipped and
# named by exactly the reason that applies to each.
if run_gt c1 0; then
  pass "C1 conjunto por omisión (graph.targets): exit 0"
  ok=1
  for artifact in merged-graph.json merged-graph.provenance.json merged-graph.report.md; do
    [ -s "$LAST_OUT/$artifact" ] || { fail "C1 falta el artefacto $artifact"; ok=0; }
  done
  [ "$ok" -eq 1 ] && pass "C1 los tres artefactos existen y no están vacíos"

  C1_PROV="$LAST_OUT/merged-graph.provenance.json"
  if [ "$(prov_get "$C1_PROV" graph_targets_summary)" = '{"declared": 6, "fused": 2, "skipped": 4}' ]; then
    pass "C1 graph_targets_summary: 6 declarados, 2 fusionados, 4 omitidos"
  else
    fail "C1 graph_targets_summary inesperado: $(prov_get "$C1_PROV" graph_targets_summary)"
  fi
  tags="$(prov_get "$C1_PROV" sources | python3 -c 'import json,sys; print(sorted(s["tag"] for s in json.load(sys.stdin)))')"
  if [ "$tags" = "['alpha', 'beta']" ]; then
    pass "C1 se fusionaron exactamente alpha y beta"
  else
    fail "C1 tags fusionados inesperados: $tags"
  fi
  counts="$(skip_stage_counts "$C1_PROV")"
  if [ "$counts" = "1 1 2" ]; then
    pass "C1 los 4 omitidos se reparten 1 sin grafo, 1 obsoleto, 2 sin resolver"
  else
    fail "C1 reparto de omitidos inesperado (missing_graph stale_skip unresolved): $counts"
  fi
  if rg -q 'no graph built yet' "$LAST_LOG" && rg -q 'skipping stale auto-discovered source' "$LAST_LOG" \
      && rg -q 'unresolved on this machine' "$LAST_LOG"; then
    pass "C1 el log nombra las tres razones de omisión"
  else
    fail "C1 el log no nombra alguna de las tres razones de omisión"
  fi
  if rg -qF 'gamma:.' "$LAST_OUT/merged-graph.report.md" \
      && rg -qF 'delta:.' "$LAST_OUT/merged-graph.report.md"; then
    pass "C1 el reporte .md lista los objetivos declarados sin fusionar"
  else
    fail "C1 el reporte .md no lista los objetivos omitidos"
  fi
else
  fail "C1 esperaba exit 0, obtuvo $LAST_RC"
fi

# C2 — --allow-stale includes the stale auto-discovered source instead of skipping
# it: the gate degrades to a warning, same as an explicit --source always could.
if run_gt c2 0 --allow-stale; then
  C2_PROV="$LAST_OUT/merged-graph.provenance.json"
  if [ "$(prov_get "$C2_PROV" graph_targets_summary.fused)" = "3" ]; then
    pass "C2 --allow-stale: delta se fusiona, 3 insumos en total"
  else
    fail "C2 --allow-stale no incluyó a delta: $(prov_get "$C2_PROV" graph_targets_summary)"
  fi
  delta_fresh="$(prov_get "$C2_PROV" sources | python3 -c 'import json,sys
for s in json.load(sys.stdin):
    if s["tag"] == "delta":
        print(s["fresh"])')"
  if [ "$delta_fresh" = "False" ]; then
    pass "C2 delta se fusiona marcado fresh:false, no se le miente al sidecar"
  else
    fail "C2 fresh de delta inesperado: $delta_fresh"
  fi
else
  fail "C2 esperaba exit 0 con --allow-stale, obtuvo $LAST_RC"
fi

# C3 — a declared repository whose HEAD cannot be read is fatal even when
# auto-discovered: "I could not look" is never downgraded to a soft skip, only
# "I looked and it was not ready yet" is. A fake .git with no real repository
# behind it reproduces the unreadable-HEAD case from A15, through discovery
# instead of an explicit --source. The pin also exercises CD_REPO_<ID>
# precedence over the root that would otherwise resolve delta.
C3="$ROOT/fixtures/c3-broken-delta/delta"
mkdir -p "$C3/graphify-out" "$C3/.git"
python3 -c '
import json, sys
data = {"directed": False, "multigraph": False, "graph": {}, "nodes": [], "links": [],
        "hyperedges": [], "built_at_commit": "1" * 40}
json.dump(data, open(sys.argv[1], "w", encoding="utf-8"))
' "$C3/graphify-out/graph.json"
mkdir -p "$ROOT/c3"
c3_rc=0
env -i PATH="$PATH" HOME="$HOME" XDG_CONFIG_HOME="$GT/xdg-config" \
    PYTHONPATH="$GT_ROOT/src" VAULT_PATH="$GT_VAULT" \
    CD_CHECKOUTS_ROOT="$GT_CHECKOUTS" \
    CD_ABSENT_ROOT="$GT/no-such-absent-root" \
    CD_REPO_DELTA="$C3" \
    /bin/bash "$SUT" --out "$ROOT/c3/out" > "$ROOT/c3/log.txt" 2>&1 || c3_rc=$?
if [ "$c3_rc" -eq 2 ]; then
  if rg -q "cannot read HEAD" "$ROOT/c3/log.txt"; then
    pass "C3 repositorio auto-descubierto con HEAD ilegible: exit 2, no un skip silencioso"
  else
    fail "C3 exit 2 pero sin el mensaje esperado"
  fi
  if [ -f "$ROOT/c3/out/merged-graph.json" ]; then
    fail "C3 publicó sin poder comprobar la frescura de un insumo declarado"
  else
    pass "C3 no publicó nada con un insumo declarado ilegible"
  fi
else
  fail "C3 esperaba exit 2, obtuvo $c3_rc"
fi

# C4 — fewer than two candidates survive discovery: both roots point nowhere and
# only alpha is pinned, so beta/gamma/delta/epsilon stay unresolved and the "at
# least two" gate fires before any merge is attempted.
c4_rc=0
mkdir -p "$ROOT/c4"
env -i PATH="$PATH" HOME="$HOME" XDG_CONFIG_HOME="$GT/xdg-config" \
    PYTHONPATH="$GT_ROOT/src" VAULT_PATH="$GT_VAULT" \
    CD_CHECKOUTS_ROOT="$GT/no-such-checkouts-root" \
    CD_ABSENT_ROOT="$GT/no-such-absent-root" \
    CD_REPO_ALPHA="$GT_CHECKOUTS/alpha" \
    /bin/bash "$SUT" --out "$ROOT/c4/out" > "$ROOT/c4/log.txt" 2>&1 || c4_rc=$?
if [ "$c4_rc" -eq 2 ]; then
  if rg -q "at least two source graphs are required; got 1" "$ROOT/c4/log.txt"; then
    pass "C4 un solo candidato tras el descubrimiento: exit 2, mensaje exacto"
  else
    fail "C4 exit 2 pero sin el mensaje esperado"
  fi
else
  fail "C4 esperaba exit 2, obtuvo $c4_rc"
fi

# --------------------------------------------------------------------------- #
# Group D — the credential scanner on its own
#
# One fixture line per shape. Running the scanner directly is what makes it cheap to
# have a canary per RULE instead of a canary per gate: the end-to-end cases above
# already prove the gate wires it up.
# --------------------------------------------------------------------------- #
printf '\n== D. Escáner de credenciales, regla por regla ==\n'

D_DIR="$ROOT/scan"
mkdir -p "$D_DIR"
D_N=0

# scan_case <expected-exit> <expected-rule|-> <description> <line>
scan_case() {
  local expected="$1" rule="$2" desc="$3" line="$4"
  D_N=$((D_N + 1))
  local file="$D_DIR/case-$D_N.txt"
  local out
  local rc=0
  printf '%s\n' "$line" > "$file"
  out="$(python3 "$SCANNER" "$file" 2>&1)" || rc=$?
  if [ "$rc" -ne "$expected" ]; then
    fail "D$D_N $desc: esperaba exit $expected, obtuvo $rc"
    return
  fi
  if [ "$rule" != "-" ]; then
    if ! printf '%s' "$out" | rg -q "$rule"; then
      fail "D$D_N $desc: exit $expected pero la regla no fue $rule"
      return
    fi
  fi
  if [ "$expected" -eq 1 ] && printf '%s' "$out" | rg -qF "$OPAQUE_CANARY"; then
    fail "D$D_N $desc: el escáner imprimió el valor"
    return
  fi
  if [ "$rule" = "-" ]; then
    pass "D$D_N $desc: exit $expected"
  else
    pass "D$D_N $desc: exit $expected ($rule)"
  fi
}

# Positives, one per rule.
scan_case 1 aws_access_key_id "clave AWS con prefijo" \
  "  \"label\": \"AKIA$(printf '%s' 'CANARY' 'FAKE')000000\","
scan_case 1 github_token "token de GitHub con prefijo" \
  "  \"label\": \"ghp_$(printf '%s' 'C4n4RyF4k3' 'C4n4RyF4k3' 'C4n4RyF4k3' 'C4n4Ry')\","
scan_case 1 private_key_block "cabecera de clave privada PEM" \
  "  \"label\": \"-----BEGIN RSA PRIVATE KEY-----\","
scan_case 1 basic_auth_in_url "credencial embebida en una URL" \
  "  \"label\": \"https://svc:$SHORT_CANARY@mf.example.invalid/checkemail\","
scan_case 1 legacy_auth_param "authpt= sin prefijo ni palabra clave" \
  "  \"label\": \"authpt=$SHORT_CANARY\","
scan_case 1 legacy_auth_param "authtoken: con valor sin comillas" \
  "  \"label\": \"authtoken: $SHORT_CANARY\","
scan_case 1 assigned_secret_literal "password= sin comillas alrededor del valor" \
  "  \"label\": \"password=$SHORT_CANARY\","
scan_case 1 assigned_secret_literal "api_key: sin comillas alrededor del valor" \
  "  \"label\": \"api_key: $SHORT_CANARY\","
scan_case 1 url_query_opaque_value "parámetro de URL con nombre que ninguna lista trae" \
  "  \"label\": \"https://mf.example.invalid/checkemail?ticket=$SHORT_CANARY\","
scan_case 1 opaque_high_entropy_value "valor opaco de 40 caracteres, sin contexto" \
  "  \"label\": \"$OPAQUE_CANARY\","

# Negatives: shapes that must stay published.
scan_case 0 - "nombre de variable de entorno" "  \"label\": \"JIRA_API_TOKEN\","
scan_case 0 - "otro nombre de variable de entorno" "  \"label\": \"CONFLUENCE_API_TOKEN\","
scan_case 0 - "identificador camelCase de 40 caracteres" \
  "  \"label\": \"isOutgoingServer2serverGroupShareEnabled\","
# El caso que la heurística sí marcaba: un método PHPUnit real del árbol
# `user_ldap` de Nextcloud, con tasa de transición 0,471 (sobre el umbral de
# 0,45) y solo 3,976 bits de entropía. Es el único falso positivo del grafo
# fusionado y bloqueaba su publicación entera; queda fijado para que no vuelva.
scan_case 0 - "método camelCase denso con acrónimo y dígito" \
  "  \"label\": \".testLoginName2UserNameNoUsersOnLDAP()\","
scan_case 0 - "identificador con acrónimo y versión, mismo perfil" \
  "  \"label\": \".getOCSApiV2ResponseForSharedFileById()\","
scan_case 0 - "digest md5 en minúsculas" "  \"label\": \"d41d8cd98f00b204e9800998ecf8427e\","
scan_case 0 - "digest sha1 en minúsculas" \
  "  \"label\": \"da39a3ee5e6b4b0d3255bfef95601890afd80709\","
scan_case 0 - "interpolación, que es justo la práctica que el gate promueve" \
  "  \"label\": \"api_key: \${JIRA_API_TOKEN}\","
scan_case 0 - "un conteo, no un secreto" "  \"label\": \"token_count = 42\","

# Empty input is a clean scan, not an error.
: > "$D_DIR/empty.txt"
d_rc=0
python3 "$SCANNER" "$D_DIR/empty.txt" > /dev/null 2>&1 || d_rc=$?
if [ "$d_rc" -eq 0 ]; then
  pass "D archivo vacío: exit 0"
else
  fail "D archivo vacío: esperaba exit 0, obtuvo $d_rc"
fi

# "No pude mirar" has its own exit code, and it is not 0 and not 1.
d_rc=0
python3 "$SCANNER" "$D_DIR/no-existe.txt" > /dev/null 2>&1 || d_rc=$?
if [ "$d_rc" -eq 2 ]; then
  pass "D archivo ilegible: exit 2, distinto de 'limpio' (0) y de 'encontré' (1)"
else
  fail "D archivo ilegible: esperaba exit 2, obtuvo $d_rc"
fi
d_rc=0
python3 "$SCANNER" > /dev/null 2>&1 || d_rc=$?
if [ "$d_rc" -eq 2 ]; then
  pass "D sin argumento: exit 2"
else
  fail "D sin argumento: esperaba exit 2, obtuvo $d_rc"
fi

printf '\n== Resumen ==\nPASS: %s  FAIL: %s\n' "$PASS_N" "$FAIL_N"
if [ "$FAIL_N" -gt 0 ]; then exit 1; fi
