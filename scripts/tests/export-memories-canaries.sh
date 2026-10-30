#!/bin/bash
# Canary battery for the promotion filter of scripts/export_memories.py: the
# rule that a memory already written up in a curated document stops being
# exported into `90 - Memory/`.
#
# Three obligations, each with a case that must be caught and a control that
# must be left alone — a filter that removes everything passes "it removed the
# promoted note" just as well as a correct one:
#
#   1. A note carrying a non-empty `knowledge_ref` is removed and counted.
#   2. A note with no `knowledge_ref`, or with a blank one, survives untouched
#      and still gets its normal `type: memory` / `source: memory` rewrite.
#   3. `--dry-run` reports the same numbers without deleting anything: the
#      weekly bridge is run by hand the first time, and a dry run that lies
#      about what it would delete is worse than no dry run.
#
# bash 3.2 safe: no arrays, no mapfile, nothing expanded under `set -u`.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
SCRIPT="$REPO/scripts/export_memories.py"
WORK="$(mktemp -d)"
FAILED=0

# The filter needs python-frontmatter, which lives in the repo's own venv.
# Falling back to the system interpreter keeps the battery runnable on a
# checkout that has not been set up yet — and says so instead of passing.
PYBIN="$REPO/.venv/bin/python"
if [ ! -x "$PYBIN" ]; then
  PYBIN="$(command -v python3)"
fi

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

fail() {
  echo "  [CANARY DEAD] $1"
  FAILED=$((FAILED + 1))
}

pass() { echo "  [CANARY OK]   $1"; }

if ! "$PYBIN" -c 'import frontmatter' 2> /dev/null; then
  echo "python-frontmatter no está instalado en este intérprete; la batería no puede correr."
  echo "EXPORT MEMORIES CANARY BATTERY: NOT RUN"
  exit 2
fi

# seed <dir> writes the three notes every case starts from.
seed() {
  root="$1/memory/nextcloud/decision"
  mkdir -p "$root"
  printf -- '---\nid: 1\ntype: decision\nproject: nextcloud\nknowledge_ref: "Services/Nextcloud/Architecture.md#Object Store"\n---\n\n# promoted\n' > "$root/promoted-1.md"
  printf -- '---\nid: 2\ntype: decision\nproject: nextcloud\n---\n\n# plain\n' > "$root/plain-2.md"
  printf -- '---\nid: 3\ntype: decision\nproject: nextcloud\nknowledge_ref: "   "\n---\n\n# blank ref\n' > "$root/blank-3.md"
}

# run_normalise <memory-root> <dry-run: 0|1> prints the stats dict.
run_normalise() {
  "$PYBIN" - "$SCRIPT" "$1" "$2" <<'PY'
import importlib.util, json, sys, types

script, memory_root, dry = sys.argv[1], sys.argv[2], sys.argv[3] == "1"

# The module imports the server's own config at import time, which needs the
# whole package. The filter under test needs none of it, so a stub stands in.
pkg = types.ModuleType("knowledge")
pkg.__path__ = []
cfg = types.ModuleType("knowledge.config")
cfg.VAULT_PATH = ""
cfg.MEMORY_FOLDER = ""
cfg.MEMORY_EXPORT_TYPES = ["decision", "discovery"]
pkg.config = cfg
sys.modules["knowledge"] = pkg
sys.modules["knowledge.config"] = cfg

spec = importlib.util.spec_from_file_location("export_memories", script)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from pathlib import Path
stats = mod.normalise(Path(memory_root), {"decision", "discovery"}, dry_run=dry)
print(json.dumps(stats))
PY
}

exists() { [ -f "$1" ] && echo yes || echo no; }

echo "== E1: una nota promovida se descarta y se cuenta =="
ROOT="$WORK/e1"
seed "$ROOT"
OUT=$(run_normalise "$ROOT" 0 | tail -1)
echo "      | $OUT"
if echo "$OUT" | grep -q '"promoted": 1'; then
  pass "E1 -> promoted = 1"
else
  fail "E1 -> el filtro no contó la nota promovida ($OUT)"
fi
if [ "$(exists "$ROOT/memory/nextcloud/decision/promoted-1.md")" = "no" ]; then
  pass "E1 -> la nota promovida ya no está en 90 - Memory/"
else
  fail "E1 -> la nota promovida sigue exportada"
fi

echo
echo "== E2: las notas sin knowledge_ref sobreviven y se reescriben =="
if [ "$(exists "$ROOT/memory/nextcloud/decision/plain-2.md")" = "yes" ]; then
  pass "E2 -> la nota sin knowledge_ref sigue ahí"
else
  fail "E2 -> el filtro se llevó una nota que no estaba promovida"
fi
if [ "$(exists "$ROOT/memory/nextcloud/decision/blank-3.md")" = "yes" ]; then
  pass "E2 -> un knowledge_ref en blanco no cuenta como promoción"
else
  fail "E2 -> un knowledge_ref en blanco se tomó por promoción"
fi
if grep -q '^type: memory$' "$ROOT/memory/nextcloud/decision/plain-2.md" \
  && grep -q '^source: memory$' "$ROOT/memory/nextcloud/decision/plain-2.md"; then
  pass "E2 -> la normalización habitual se sigue aplicando"
else
  fail "E2 -> la nota superviviente no recibió type: memory / source: memory"
fi
if echo "$OUT" | grep -q '"kept": 2'; then
  pass "E2 -> kept = 2"
else
  fail "E2 -> kept no vale 2 ($OUT)"
fi

echo
echo "== E3: --dry-run informa lo mismo sin borrar nada =="
ROOT="$WORK/e3"
seed "$ROOT"
DRY=$(run_normalise "$ROOT" 1 | tail -1)
echo "      | $DRY"
if echo "$DRY" | grep -q '"promoted": 1'; then
  pass "E3 -> el dry-run también cuenta la promoción"
else
  fail "E3 -> el dry-run no contó la promoción ($DRY)"
fi
if [ "$(exists "$ROOT/memory/nextcloud/decision/promoted-1.md")" = "yes" ]; then
  pass "E3 -> el dry-run no borró la nota"
else
  fail "E3 -> el dry-run BORRÓ la nota promovida"
fi
if grep -q '^type: decision$' "$ROOT/memory/nextcloud/decision/plain-2.md"; then
  pass "E3 -> el dry-run tampoco reescribió el frontmatter"
else
  fail "E3 -> el dry-run reescribió un archivo"
fi

echo
echo "== E4: sin carpeta exportada el filtro no inventa nada =="
ROOT="$WORK/e4"
mkdir -p "$ROOT"
EMPTY=$(run_normalise "$ROOT" 0 | tail -1)
echo "      | $EMPTY"
if echo "$EMPTY" | grep -q '"promoted": 0'; then
  pass "E4 -> promoted = 0 cuando no hay nada exportado"
else
  fail "E4 -> el filtro reportó promociones sobre una carpeta vacía ($EMPTY)"
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "EXPORT MEMORIES CANARY BATTERY: PASS (9/9)"
else
  echo "EXPORT MEMORIES CANARY BATTERY: FAIL ($FAILED canario(s) en rojo)"
fi
exit "$FAILED"
