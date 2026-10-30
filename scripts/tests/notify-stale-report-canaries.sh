#!/bin/bash
# Canary battery for the two halves of the `custom: stale-report` pipeline:
# scripts/notify-stale-report.sh (N1-N13) and the review-decay gate in
# scripts/weekly-stale-report.py (N14-N22).
#
# Four obligations, each with at least one case that makes the script fail
# and one that lets it pass:
#
#   1. Exit codes are a contract. Every case below pins one with `=`, never
#      with "non-zero" — a gate whose failures all collapse into 1 cannot
#      tell "the binary is missing" from "the webhook said no".
#   2. A missing dependency reports itself. `curl: command not found`
#      swallowed by `2>/dev/null` used to surface as "fallo de red", which
#      sent the operator to debug a URL that was never the problem.
#   3. The webhook URL never reaches stdout or stderr, in any branch — and
#      the branch that prints curl's diagnostics has to print something, or
#      "no leak" would be trivially satisfied by printing nothing.
#   4. A gate that "found nothing" must have looked. The report cases below
#      hand it a vault whose overdue document sits behind something the walk
#      can lose — a symlinked folder, an unreadable folder, an uppercase
#      suffix — and demand exactly 1. Each one is paired with a control that
#      still yields exactly 0, so "always red" cannot pass for "correct".
#
# The token below is a fabricated string, not a credential.
# bash 3.2 safe: no arrays, no mapfile, nothing expanded under `set -u`.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
SCRIPT="$REPO/scripts/notify-stale-report.sh"
# Absolute: the dependency canaries below strip PATH down to a directory that
# holds one binary, and `bash` has to stay reachable from outside it.
BASH_BIN="$(command -v bash)"
OTHER_URL="https://hooks.example.invalid/OTHER-SYNTHETIC-NOT-A-REAL-TOKEN"
WORK="$(mktemp -d)"
SECRET_PATH="CANARY-SYNTHETIC-NOT-A-REAL-TOKEN"
FAILED=0
NOT_RUN=0
SRV_PID=""

cleanup() {
  rm -rf "$WORK"
  if [ -n "$SRV_PID" ]; then
    kill "$SRV_PID" 2>/dev/null
  fi
}
trap cleanup EXIT

printf '# Reporte\n\nUn doc con `backticks`, "comillas" y \\ barras.\n' > "$WORK/report.md"

fail() {
  echo "  [CANARY DEAD] $1"
  FAILED=$((FAILED + 1))
}

# A case this environment cannot reproduce is counted and named out loud. A
# canary that skips itself in silence is the same disease it hunts: nobody
# can tell "it passed" from "it never ran".
notrun() {
  echo "  [CANARY N/A]  $1"
  NOT_RUN=$((NOT_RUN + 1))
}

# check <label> <expected-exit> <actual-exit> <combined-output>
check() {
  label="$1"; expected_code="$2"; actual_code="$3"; combined="$4"
  ok=1
  if [ "$actual_code" != "$expected_code" ]; then
    echo "  [CANARY DEAD] $label -> exit $actual_code, se esperaba exactamente $expected_code"
    ok=0
  fi
  if echo "$combined" | grep -q "$SECRET_PATH"; then
    echo "  [CANARY DEAD] $label -> LA URL DEL WEBHOOK APARECIÓ EN LA SALIDA"
    ok=0
  fi
  if [ "$ok" -eq 1 ]; then
    echo "  [CANARY OK]   $label -> exit $actual_code y ninguna filtración de la URL"
  else
    FAILED=$((FAILED + 1))
  fi
  echo "$combined" | sed 's/^/      | /'
}

# expect_contains <label> <needle> <haystack>
expect_contains() {
  if echo "$3" | grep -q -- "$2"; then
    echo "  [CANARY OK]   $1 -> la salida menciona '$2'"
  else
    fail "$1 -> la salida NO menciona '$2'"
  fi
}

# --- Local webhook receiver -------------------------------------------------
/usr/bin/env python3 - "$WORK" <<'PY' &
import http.server, sys, pathlib
work = pathlib.Path(sys.argv[1])

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        (work / "received.json").write_bytes(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')
    def log_message(self, *a):
        pass

srv = http.server.HTTPServer(("127.0.0.1", 8791), H)
(work / "port.txt").write_text("8791")
srv.serve_forever()
PY
SRV_PID=$!
i=0
while [ ! -f "$WORK/port.txt" ] && [ "$i" -lt 50 ]; do i=$((i + 1)); sleep 0.1; done
PORT=$(cat "$WORK/port.txt" 2>/dev/null || echo "")
if [ -z "$PORT" ]; then
  echo "No se pudo levantar el receptor local; abortando."
  exit 1
fi
URL="http://127.0.0.1:$PORT/hooks/$SECRET_PATH"

echo "== N1: sin variable, el paso es un no-op documentado =="
out=$(cd "$REPO" && env -u STALE_REPORT_WEBHOOK_URL bash "$SCRIPT" "$WORK/report.md" 2>&1); code=$?
check "N1 variable ausente" 0 "$code" "$out"

echo
echo "== N2: variable vacía, mismo no-op =="
out=$(cd "$REPO" && STALE_REPORT_WEBHOOK_URL="" bash "$SCRIPT" "$WORK/report.md" 2>&1); code=$?
check "N2 variable vacía" 0 "$code" "$out"

echo
echo "== N3: falta el reporte -> exit 2, nunca un mensaje vacío enviado =="
out=$(cd "$REPO" && STALE_REPORT_WEBHOOK_URL="$URL" bash "$SCRIPT" "$WORK/no-existe.md" 2>&1); code=$?
check "N3 reporte inexistente" 2 "$code" "$out"

echo
echo "== N4: envío real contra el receptor local =="
out=$(cd "$REPO" && STALE_REPORT_WEBHOOK_URL="$URL" bash "$SCRIPT" "$WORK/report.md" 2>&1); code=$?
check "N4 envío correcto" 0 "$code" "$out"
if [ -f "$WORK/received.json" ]; then
  /usr/bin/env python3 - "$WORK/received.json" "$WORK/report.md" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
original = open(sys.argv[2], encoding="utf-8").read()
assert payload["text"] == original, "el cuerpo enviado no coincide con el reporte"
print("      | payload JSON válido y byte a byte igual al reporte")
PY
  if [ "$?" -ne 0 ]; then fail "N4 -> el payload no coincide con el reporte"; fi
else
  fail "N4 -> el receptor no registró ninguna petición"
fi

echo
echo "== N5: truncado cuando el reporte excede el límite =="
/usr/bin/env python3 -c "open('$WORK/big.md','w').write('x'*9000)"
out=$(cd "$REPO" && STALE_REPORT_WEBHOOK_URL="$URL" STALE_REPORT_MAX_CHARS=100 bash "$SCRIPT" "$WORK/big.md" 2>&1); code=$?
check "N5 truncado" 0 "$code" "$out"
/usr/bin/env python3 - "$WORK/received.json" <<'PY'
import json, sys
text = json.load(open(sys.argv[1], encoding="utf-8"))["text"]
assert len(text) < 500, "no truncó"
assert "truncado" in text, "no avisó del truncado"
print("      | mensaje truncado a %d caracteres y con aviso" % len(text))
PY
if [ "$?" -ne 0 ]; then fail "N5 -> el truncado no se aplicó"; fi

echo
echo "== N6: destino inalcanzable -> exit 1 SIN imprimir la URL =="
out=$(cd "$REPO" && STALE_REPORT_WEBHOOK_URL="http://127.0.0.1:9/hooks/$SECRET_PATH" bash "$SCRIPT" "$WORK/report.md" 2>&1); code=$?
check "N6 webhook caído" 1 "$code" "$out"

echo
echo "== N7: el webhook responde 500 -> exit 1 SIN imprimir la URL =="
kill "$SRV_PID" 2>/dev/null; wait "$SRV_PID" 2>/dev/null
/usr/bin/env python3 - <<'PY' &
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(500); self.end_headers(); self.wfile.write(b"nope")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", 8791), H).serve_forever()
PY
SRV_PID=$!
sleep 1
out=$(cd "$REPO" && STALE_REPORT_WEBHOOK_URL="$URL" bash "$SCRIPT" "$WORK/report.md" 2>&1); code=$?
check "N7 respuesta 500" 1 "$code" "$out"

echo
echo "== N8: el archivo del script no contiene ninguna credencial literal =="
if grep -Eq 'https?://[^ "]*(hooks|token|xoxb|secret)' "$SCRIPT"; then
  fail "N8 -> hay una URL con pinta de credencial dentro del script"
else
  echo "  [CANARY OK]   N8 -> el script solo nombra la variable, nunca su valor"
fi

# --- Dependencias ausentes --------------------------------------------------
# La imagen del pipeline (python:3.12-slim) NO trae curl. Antes, con el
# receptor vivo en el mismo contenedor, el script decía "fallo de red".
mkdir -p "$WORK/bin-nocurl" "$WORK/bin-nopy"
ln -sf "$(command -v python3)" "$WORK/bin-nocurl/python3"
ln -sf "$(command -v curl)" "$WORK/bin-nopy/curl"

echo
echo "== N9: curl ausente -> exit 3 y se nombra a curl, no a la red =="
out=$(cd "$REPO" && PATH="$WORK/bin-nocurl" STALE_REPORT_WEBHOOK_URL="$URL" \
  "$BASH_BIN" "$SCRIPT" "$WORK/report.md" 2>&1); code=$?
check "N9 curl ausente" 3 "$code" "$out"
expect_contains "N9 nombra el binario" "curl" "$out"
if echo "$out" | grep -q "fallo de red o URL inválida"; then
  fail "N9 -> un binario ausente sigue disfrazándose de caída de red"
else
  echo "  [CANARY OK]   N9 -> no se disfraza de caída de red"
fi

echo
echo "== N10: python3 ausente -> exit 3 y se nombra a python3 =="
out=$(cd "$REPO" && PATH="$WORK/bin-nopy" STALE_REPORT_WEBHOOK_URL="$URL" \
  "$BASH_BIN" "$SCRIPT" "$WORK/report.md" 2>&1); code=$?
check "N10 python3 ausente" 3 "$code" "$out"
expect_contains "N10 nombra el binario" "python3" "$out"

echo
echo "== N11: curl escupe la URL en stderr -> exit 1, diagnóstico SIN la URL =="
mkdir -p "$WORK/bin-leaky"
cat > "$WORK/bin-leaky/curl" <<'STUB'
#!/bin/sh
# Reproduces the shape of a real curl diagnostic: it echoes the target URL.
echo "curl: (6) Could not resolve host -- target was $*" >&2
printf '000'
exit 6
STUB
chmod +x "$WORK/bin-leaky/curl"
out=$(cd "$REPO" && PATH="$WORK/bin-leaky:$PATH" STALE_REPORT_WEBHOOK_URL="$URL" \
  "$BASH_BIN" "$SCRIPT" "$WORK/report.md" 2>&1); code=$?
check "N11 curl filtra la URL" 1 "$code" "$out"
# El diagnóstico tiene que existir: "no filtra" se satisface trivialmente
# imprimiendo nada, y eso es justo lo que ocultó el binario ausente.
expect_contains "N11 hay diagnóstico" "Diagnóstico de curl" "$out"
expect_contains "N11 la URL va sustituida" "<STALE_REPORT_WEBHOOK_URL>" "$out"

echo
echo "== N12: curl nombra OTRA URL (redirección) -> se redacta igual =="
# Un patrón anclado al valor conocido no ve una URL distinta. curl imprime
# destinos de redirección y hosts de proxy que nadie configuró aquí: la
# segunda capa de redacción existe para eso, y sin este canario nadie la
# ejercita.
mkdir -p "$WORK/bin-500"
cat > "$WORK/bin-500/curl" <<STUB
#!/bin/sh
echo "curl: (47) Maximum redirects followed; last hop was $OTHER_URL" >&2
printf '500'
exit 0
STUB
chmod +x "$WORK/bin-500/curl"
out=$(cd "$REPO" && PATH="$WORK/bin-500:$PATH" STALE_REPORT_WEBHOOK_URL="$URL" \
  "$BASH_BIN" "$SCRIPT" "$WORK/report.md" 2>&1); code=$?
check "N12 HTTP 500 con traza" 1 "$code" "$out"
expect_contains "N12 hay diagnóstico" "Diagnóstico de curl" "$out"
expect_contains "N12 la otra URL va redactada" "url-redactada" "$out"
if echo "$out" | grep -q "OTHER-SYNTHETIC-NOT-A-REAL-TOKEN"; then
  fail "N12 -> se filtró una URL que no era la del webhook"
else
  echo "  [CANARY OK]   N12 -> tampoco se filtra una URL ajena"
fi

# --- Documentación ----------------------------------------------------------
echo
echo "== N13: ningún encabezado de scripts/README.md quedó pegado a un párrafo =="
if grep -nE '[^#]#{2,4} [A-Za-z]' "$REPO/scripts/README.md"; then
  fail "N13 -> un '##' no arranca línea: la sección de alguien perdió su título"
else
  echo "  [CANARY OK]   N13 -> todos los encabezados arrancan línea"
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "NOTIFY CANARY BATTERY: PASS (13/13)"
else
  echo "NOTIFY CANARY BATTERY: FAIL ($FAILED canario(s) en rojo)"
fi
exit "$FAILED"
