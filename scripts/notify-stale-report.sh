#!/usr/bin/env bash
# Post the weekly stale-doc report to a chat webhook.
#
# The destination is read from the STALE_REPORT_WEBHOOK_URL repository
# variable (Bitbucket: Repository settings -> Repository variables, "Secured"
# checked). The URL is a credential: it reaches curl through the environment
# and is never written to a file, an argument list that gets echoed, or the
# job log — not even when the request fails. curl's own diagnostics are
# surfaced, but only after the URL is stripped out of them.
#
# Without the variable the script is a documented no-op, so the scheduled
# pipeline stays green until someone provisions the webhook.
#
# Usage: scripts/notify-stale-report.sh [report-file]
#
# Exit codes:
#   0  sent, or a documented no-op (no webhook configured)
#   1  the webhook was reachable but the delivery failed (non-2xx, or curl
#      could not complete the request)
#   2  the report file to send does not exist
#   3  a binary this script depends on is missing from the image — "I could
#      not look", which is never the same as "nothing to report"
#
# Written for bash 3.2 (macOS /bin/bash) and for the Linux images Pipelines
# runs: no arrays, no `mapfile`, safe under `set -u`.
set -eu

REPORT_FILE="${1:-reports/stale-report.md}"
MAX_CHARS="${STALE_REPORT_MAX_CHARS:-3500}"

EXIT_DELIVERY_FAILED=1
EXIT_NO_REPORT=2
EXIT_MISSING_TOOL=3

# --- Dependencies -----------------------------------------------------------
# Checked before anything else, including before the "no webhook configured"
# no-op. A step that could not send even if it were configured is broken
# today; finding that out on the day the secret is provisioned is exactly the
# failure this guard exists to prevent. And a missing binary must report
# itself: `curl: command not found` swallowed into a generic network error
# sends the operator to debug a URL that was never the problem.
require_tool() {
  if command -v "$1" >/dev/null 2>&1; then
    return 0
  fi
  echo "ERROR: falta el binario '$1' en esta imagen; el aviso no puede enviarse." >&2
  echo "       No es un fallo de red ni una URL inválida: el programa no existe." >&2
  echo "       Instálalo en el paso del pipeline (apt-get install -y $1)." >&2
  exit "$EXIT_MISSING_TOOL"
}

require_tool curl
require_tool python3

if [ -z "${STALE_REPORT_WEBHOOK_URL:-}" ]; then
  echo "Notificación desactivada: STALE_REPORT_WEBHOOK_URL no está definida."
  echo "Defínela como variable de repositorio protegida para avisar a los dueños."
  exit 0
fi

if [ ! -f "$REPORT_FILE" ]; then
  echo "ERROR: no existe el reporte a enviar: $REPORT_FILE" >&2
  exit "$EXIT_NO_REPORT"
fi

WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT
CURL_STDERR="$WORK_DIR/curl.err"

# Strip the webhook URL out of any text before it reaches the log. The secret
# is handed to python through the environment, never through argv, so it
# cannot show up in a process listing either.
redact() {
  STALE_REPORT_WEBHOOK_URL="$STALE_REPORT_WEBHOOK_URL" python3 - "$1" <<'PY'
import os
import re
import sys

secret = os.environ.get("STALE_REPORT_WEBHOOK_URL", "")
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as handle:
        text = handle.read()
except OSError:
    text = ""
if secret:
    text = text.replace(secret, "<STALE_REPORT_WEBHOOK_URL>")
# Belt and braces: curl can print a normalised or partially escaped form of
# the URL, which the literal replacement above would miss.
text = re.sub(r"https?://\S+", "<url-redactada>", text)
sys.stdout.write(" ".join(text.split()))
PY
}

# json.dumps handles the quoting and the escaping of a Markdown body; building
# the payload with shell string concatenation breaks on the first backtick.
PAYLOAD=$(REPORT_FILE="$REPORT_FILE" MAX_CHARS="$MAX_CHARS" python3 - <<'PY'
import json
import os

path = os.environ["REPORT_FILE"]
limit = int(os.environ["MAX_CHARS"])
body = open(path, encoding="utf-8").read()
if len(body) > limit:
    body = body[:limit] + "\n\n… reporte truncado; el completo está en los artefactos del build."
print(json.dumps({"text": body}))
PY
)

# curl's diagnostics go to a file instead of /dev/null: dropping them is what
# turned "the binary is missing" into "the network failed". They are printed
# only after redact() removes the URL.
set +e
HTTP_CODE=$(printf '%s' "$PAYLOAD" | curl -sS -o /dev/null -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' --data-binary @- \
  "$STALE_REPORT_WEBHOOK_URL" 2>"$CURL_STDERR")
CURL_STATUS=$?
set -e

report_curl_diagnostics() {
  # A failure inside redact() must not replace the real error with its own,
  # and its stderr is dropped rather than risk echoing anything unredacted.
  detail=$(redact "$CURL_STDERR" 2>/dev/null || echo "")
  if [ -n "$detail" ]; then
    echo "       Diagnóstico de curl (URL redactada): $detail" >&2
  fi
}

case "$HTTP_CODE" in
  2*)
    echo "Reporte enviado al webhook (HTTP $HTTP_CODE)."
    ;;
  "" | 000)
    echo "ERROR: curl no completó la petición (salió con código $CURL_STATUS)." >&2
    report_curl_diagnostics
    exit "$EXIT_DELIVERY_FAILED"
    ;;
  *)
    echo "ERROR: el webhook respondió HTTP $HTTP_CODE." >&2
    report_curl_diagnostics
    exit "$EXIT_DELIVERY_FAILED"
    ;;
esac
