#!/usr/bin/env bash
# validate-runbook.sh — checks a vault runbook against ai-platform-plan/05-runbooks/plantilla-runbook.md
# section 1 (frontmatter), section 2 (body sections) and the check table of section 6.
#
# Usage:
#   validate-runbook.sh <file.md> [file2.md ...]
#   validate-runbook.sh                          # scans MEMORY_VAULT_ROOT/Runbooks for type: runbook
#
# Output: one [PASS]/[FAIL]/[WARN] line per check per file, indented details below it.
# Exit code: 1 when at least one file has at least one FAIL, 0 otherwise.
# Tools: bash, rg, yq, jq, mmdc (mermaid-cli, optional -- check 7 warns instead of failing when
#        it is not on PATH). Compatible with the bash 3.2 shipped by macOS (no mapfile, no
#        associative arrays, no ${var,,}).
set -u

# shellcheck source=lib/guards.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/guards.sh"

VAULT_ROOT="${MEMORY_VAULT_ROOT:-$(runbook_vault_root || true)}"
if [[ -z "$VAULT_ROOT" ]]; then
  printf 'SKIP: no se resolvió la raíz del vault desde %s; exporta MEMORY_VAULT_ROOT\n' "${BASH_SOURCE[0]}" >&2
  exit 77
fi

PASS_N=0
FAIL_N=0
WARN_N=0

pass() { PASS_N=$((PASS_N + 1)); printf '  [PASS] %s\n' "$*"; }
fail() { FAIL_N=$((FAIL_N + 1)); printf '  [FAIL] %s\n' "$*"; }
warn() { WARN_N=$((WARN_N + 1)); printf '  [WARN] %s\n' "$*"; }
detail() {
  local text="$1"
  [[ -z "$text" ]] && return 0
  while IFS= read -r line; do
    [[ -n "$line" ]] && printf '         %s\n' "$line"
  done <<< "$text"
}
count_lines() { if [[ -s "$1" ]]; then wc -l < "$1" | tr -d ' '; else printf '0'; fi; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/validate-runbook.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# --------------------------------------------------------------------------- #
# Static data: plantilla-runbook.md section 1 (frontmatter) and section 2 (body)
# --------------------------------------------------------------------------- #

REQUIRED_FIELDS="type id title service severity category pattern status owner automation_level \
last_verified last_updated last_occurrence symptoms root_cause resolution_time affected_services \
affected_endpoints affected_tables affected_databases jira_project jira_issuetype jira_issuetype_id \
jira_refs country partner tools evidence_required tags"

# Allowed values of the frontmatter `service:` field.
#
# A closed catalog is still the right check here: the field exists so a runbook
# can be found by the service it belongs to, and the only defect that hurts is
# a value that looks fine and matches nothing -- "midleware", or a service
# somebody invented in the frontmatter. Membership in a list is what turns that
# into a FAIL. What a closed catalog must not be is a list of one product's
# services frozen into a script every deployment shares, so the catalog is
# resolved rather than spelled here:
#
#   1. $RUNBOOK_SERVICE_ENUM, a space-separated list, for a one-off run;
#   2. `runbooks.service_enum` in this checkout's config.json;
#   3. neither -- and then check 2 WARNs on `service` rather than passing it.
#
# Case 3 is deliberately not a PASS and not a FAIL. "No catalog is configured"
# and "this value is not in the catalog" are different answers, and only the
# second one is the validator's to give; a PASS there would report a check that
# never ran. An unreadable config.json is a fourth answer and says so too.
SERVICE_ENUM=""
SERVICE_ENUM_SOURCE=""
__svc_root="$(runbook_project_root || true)"
if [[ -n "${RUNBOOK_SERVICE_ENUM:-}" ]]; then
  SERVICE_ENUM="$RUNBOOK_SERVICE_ENUM"
  SERVICE_ENUM_SOURCE="RUNBOOK_SERVICE_ENUM"
elif [[ -n "$__svc_root" && -f "$__svc_root/config.json" ]]; then
  if SERVICE_ENUM="$(jq -r '(.runbooks.service_enum // []) | join(" ")' "$__svc_root/config.json" 2> /dev/null)"; then
    SERVICE_ENUM_SOURCE="$__svc_root/config.json"
  else
    SERVICE_ENUM=""
    SERVICE_ENUM_SOURCE="ilegible:$__svc_root/config.json"
  fi
fi

SEVERITY_ENUM="P1 P2 P3 P4"
CATEGORY_ENUM="auth database queue network performance data-integrity registration"
PATTERN_ENUM="missing-files auth-access file-save-failure sync-upload registration-subscription other"
STATUS_ENUM="draft verified outdated"
AUTOMATION_ENUM="manual assisted autonomous-with-gate"
COUNTRY_ENUM="MX AR CO CL CR BR ALL"
PARTNER_ENUM="telvia orbita comet none"
CLI_TOOL_ALLOWLIST="bkt curl occ mysql-readonly jq"

# Section 2, in the order the template mandates.
CANONICAL_SECTIONS='## Symptoms
## Root Cause
## Affected Components
## Preconditions
## Solution Flow
## Validation Flow
## Rollback
## Evidence
## Communication
## Prevention
## Related Issues
## History'

# Same forbidden list as context/tools/validate.sh check 9 (idioma), scoped here to the
# Spanish "Communication" section -- the only part of a runbook the template puts in Spanish.
LANG_FORBIDDEN='vosotros|habéis|sois|ordenador|fichero|coger|laburo|boludo|quilombo|chido|ahorita|cachai'

MMDC="${MMDC:-}"
if [[ -z "$MMDC" ]]; then
  if [[ -x "$HOME/.npm-global/bin/mmdc" ]]; then MMDC="$HOME/.npm-global/bin/mmdc"
  elif command -v mmdc > /dev/null 2>&1; then MMDC="$(command -v mmdc)"
  fi
fi
PUPPETEER_CFG="${PUPPETEER_CFG:-}"
if [[ -z "$PUPPETEER_CFG" ]]; then
  __arch_root="$(runbook_arch_root || true)"
  if [[ -n "$__arch_root" && -f "$__arch_root/context/tools/puppeteer-config.json" ]]; then
    PUPPETEER_CFG="$__arch_root/context/tools/puppeteer-config.json"
  fi
fi

in_list() {
  # in_list <needle> <space-separated haystack>
  local needle="$1" hay="$2" w
  for w in $hay; do [[ "$w" == "$needle" ]] && return 0; done
  return 1
}

# --------------------------------------------------------------------------- #
# Frontmatter extraction: the block between the first two "---" lines, as JSON.
# --------------------------------------------------------------------------- #
frontmatter_json() {
  local file="$1"
  awk '
    NR == 1 && $0 == "---" { infm = 1; next }
    infm && $0 == "---" { exit }
    infm { print }
  ' "$file" | yq -o=json -p=yaml '.' - 2> /dev/null
}

body_after_frontmatter() {
  local file="$1"
  awk '
    NR == 1 && $0 == "---" { infm = 1; seen = 0; next }
    infm && $0 == "---" && !seen { seen = 1; infm = 0; next }
    !infm { print }
  ' "$file"
}

# --------------------------------------------------------------------------- #
# Vault-wide id index, built once, used by check 3 (uniqueness).
# --------------------------------------------------------------------------- #
build_vault_id_index() {
  : > "$TMP/vault_ids.txt"
  [[ -d "$VAULT_ROOT/Runbooks" ]] || return 0
  local f id
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    [[ "$f" == *"/Templates/"* ]] && continue
    rg -q -F -- 'type: runbook' "$f" 2> /dev/null || continue
    id="$(rg -o --no-filename '^id: "?(RB-[A-Za-z0-9-]+)"?$' -r '$1' "$f" 2> /dev/null | head -1)"
    [[ -z "$id" ]] && continue
    printf '%s\t%s\n' "$id" "$f" >> "$TMP/vault_ids.txt"
  done < <(fd -H -I -t f -e md . "$VAULT_ROOT/Runbooks" 2> /dev/null)
}

# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

check_frontmatter_fields() {
  local file="$1" fm_json="$2"
  printf ' -- 1. Frontmatter YAML valido y campos obligatorios --\n'
  if [[ -z "$fm_json" ]]; then
    fail "1 el frontmatter no parsea como YAML (o no hay bloque --- ... ---)"
    return 1
  fi
  if ! printf '%s' "$fm_json" | jq -e 'type == "object"' > /dev/null 2>&1; then
    fail "1 el frontmatter no es un mapa YAML"
    return 1
  fi
  local missing="" field
  for field in $REQUIRED_FIELDS; do
    if ! printf '%s' "$fm_json" | jq -e --arg f "$field" 'has($f)' > /dev/null 2>&1; then
      missing="$missing$field"$'\n'
    fi
  done
  if [[ -n "$missing" ]]; then
    fail "1 faltan $(printf '%s' "$missing" | count_lines /dev/stdin) de $(printf '%s\n' $REQUIRED_FIELDS | count_lines /dev/stdin) campos obligatorios de la tabla de la plantilla (parrafo 1)"
    detail "$missing"
    return 1
  fi
  pass "1 los $(printf '%s\n' $REQUIRED_FIELDS | count_lines /dev/stdin) campos obligatorios estan presentes"
  return 0
}

check_enums() {
  local fm_json="$1"
  printf ' -- 2. Enums dentro de su lista permitida --\n'
  local problems="" val
  scalar_check() {
    local field="$1" enum="$2"
    val="$(printf '%s' "$fm_json" | jq -r --arg f "$field" '.[$f] // empty')"
    [[ -z "$val" ]] && return 0
    in_list "$val" "$enum" || problems="$problems$field='$val' no esta en su enum"$'\n'
  }
  list_enum_check() {
    local field="$1" enum="$2" kind items item
    kind="$(printf '%s' "$fm_json" | jq -r --arg f "$field" '.[$f] | type // "null"')"
    case "$kind" in
      null) return 0 ;;
      array)
        items="$(printf '%s' "$fm_json" | jq -r --arg f "$field" '.[$f][]?')"
        while IFS= read -r item; do
          [[ -z "$item" ]] && continue
          in_list "$item" "$enum" || problems="$problems$field: '$item' no esta en su enum"$'\n'
        done <<< "$items"
        ;;
      string)
        item="$(printf '%s' "$fm_json" | jq -r --arg f "$field" '.[$f]')"
        problems="$problems$field: tipo debe ser list(enum), se encontro escalar '$item'"$'\n'
        in_list "$item" "$enum" || problems="$problems$field: ademas '$item' no esta en su enum"$'\n'
        ;;
      *)
        problems="$problems$field: tipo inesperado ($kind)"$'\n'
        ;;
    esac
  }
  local validated="severity/category/pattern/status/automation_level/country/partner"
  if [[ -n "$SERVICE_ENUM" ]]; then
    scalar_check service "$SERVICE_ENUM"
    validated="service/$validated"
  fi
  scalar_check severity "$SEVERITY_ENUM"
  scalar_check category "$CATEGORY_ENUM"
  scalar_check pattern "$PATTERN_ENUM"
  scalar_check status "$STATUS_ENUM"
  scalar_check automation_level "$AUTOMATION_ENUM"
  list_enum_check country "$COUNTRY_ENUM"
  list_enum_check partner "$PARTNER_ENUM"
  if [[ -n "$problems" ]]; then
    fail "2 $(printf '%s' "$problems" | count_lines /dev/stdin) valor(es) fuera de enum o con tipo incorrecto"
    detail "$problems"
  else
    pass "2 $validated dentro de su enum"
  fi
  if [[ -z "$SERVICE_ENUM" ]]; then
    case "$SERVICE_ENUM_SOURCE" in
      ilegible:*)
        fail "2 el catálogo de servicios no se pudo leer (${SERVICE_ENUM_SOURCE#ilegible:} no parsea como JSON); 'service' quedó sin validar"
        ;;
      *)
        warn "2 sin catálogo de servicios configurado; 'service' no se validó contra ninguna lista. Declara runbooks.service_enum en el config.json del checkout, o exporta RUNBOOK_SERVICE_ENUM=\"svc-a svc-b\" (0 no es PASS)"
        ;;
    esac
  fi
}

check_id_unique_and_aligned() {
  local file="$1" fm_json="$2"
  printf ' -- 3. id unico, coincide con archivo y con el titulo --\n'
  local id; id="$(printf '%s' "$fm_json" | jq -r '.id // empty')"
  if [[ -z "$id" ]]; then
    fail "3 no se puede verificar: falta el campo id"
    return
  fi
  local abs_file; abs_file="$(cd "$(dirname "$file")" 2> /dev/null && pwd)/$(basename "$file")"
  local n_occ=0 self_in_index=0 line row_id row_path
  while IFS=$'\t' read -r row_id row_path; do
    [[ "$row_id" == "$id" ]] || continue
    n_occ=$((n_occ + 1))
    [[ "$row_path" == "$abs_file" ]] && self_in_index=1
  done < "$TMP/vault_ids.txt"
  local effective_n="$n_occ"
  (( self_in_index == 0 )) && effective_n=$((n_occ + 1))
  if (( effective_n > 1 )); then
    fail "3 id '$id' duplicado: aparece $effective_n vez/veces en el vault"
    detail "$(rg -x -F -- "$id"$'\t''.*' "$TMP/vault_ids.txt" 2> /dev/null | cut -f2)
$( (( self_in_index == 0 )) && printf '%s (archivo bajo prueba, fuera del vault)\n' "$abs_file")"
  else
    pass "3 id '$id' es unico en el vault"
  fi

  local base; base="$(basename "$file")"
  if [[ "$base" == "$id "* || "$base" == "$id.md" ]]; then
    pass "3 el nombre de archivo empieza con '$id'"
  else
    fail "3 el nombre de archivo '$base' no empieza con el id '$id'"
  fi

  local heading; heading="$(rg -m1 -o --no-filename '^# .+$' "$file" 2> /dev/null)"
  if [[ "$heading" == "# $id:"* ]]; then
    pass "3 el titulo '$heading' coincide con '# $id:'"
  else
    fail "3 el titulo '$heading' no empieza con '# $id:'"
  fi
}

check_symptoms_and_jira_refs() {
  local fm_json="$1"
  printf ' -- 4. symptoms >= 2; jira_refs >= 1 si status: verified --\n'
  local sym_n refs_n status
  sym_n="$(printf '%s' "$fm_json" | jq -r '(.symptoms // []) | length')"
  refs_n="$(printf '%s' "$fm_json" | jq -r '(.jira_refs // []) | length')"
  status="$(printf '%s' "$fm_json" | jq -r '.status // empty')"
  local problems=""
  if (( sym_n < 2 )); then problems="${problems}symptoms tiene $sym_n entrada(s), se requieren >=2"$'\n'; fi
  if [[ "$status" == "verified" && "$refs_n" -lt 1 ]]; then
    problems="${problems}status: verified exige jira_refs >= 1, tiene $refs_n"$'\n'
  fi
  if [[ -n "$problems" ]]; then
    fail "4 listas cortas"
    detail "$problems"
  else
    pass "4 symptoms=$sym_n (>=2); jira_refs=$refs_n (status=$status)"
  fi
}

check_tools() {
  local fm_json="$1"
  printf ' -- 5. tools[] conocidas (MCP o CLI permitido) --\n'
  local kind; kind="$(printf '%s' "$fm_json" | jq -r '.tools | type // "null"')"
  if [[ "$kind" != "array" ]]; then
    warn "5 sin campo tools[] que validar (ver check 1)"
    return
  fi
  local n; n="$(printf '%s' "$fm_json" | jq -r '.tools | length')"
  if (( n == 0 )); then
    warn "5 tools[] esta vacio; nada que validar (0 no es PASS)"
    return
  fi
  local problems="" t
  while IFS= read -r t; do
    [[ -z "$t" ]] && continue
    if [[ "$t" == mcp__* ]]; then continue; fi
    in_list "$t" "$CLI_TOOL_ALLOWLIST" && continue
    problems="${problems}'$t' no es un prefijo mcp__ conocido ni esta en el allowlist de CLIs ($CLI_TOOL_ALLOWLIST)"$'\n'
  done < <(printf '%s' "$fm_json" | jq -r '.tools[]?')
  if [[ -n "$problems" ]]; then
    fail "5 $(printf '%s' "$problems" | count_lines /dev/stdin) de $n tools desconocidas"
    detail "$problems"
  else
    pass "5 las $n tools declaradas son mcp__* o CLI permitido"
  fi
}

check_sections() {
  local file="$1"
  printf ' -- 6. Secciones obligatorias, presentes y en orden --\n'
  local body; body="$(body_after_frontmatter "$file")"
  local found; found="$(printf '%s\n' "$body" | rg -o --no-filename '^## .+$' 2> /dev/null)"
  local filtered=""
  local line
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    rg -q -x -F -- "$line" <<< "$CANONICAL_SECTIONS" && filtered="$filtered$line"$'\n'
  done <<< "$found"
  # $(...) below strips its trailing newline; strip filtered's the same way so an
  # otherwise-identical sequence does not fail the comparison on that alone.
  filtered="${filtered%$'\n'}"
  local missing="" want
  while IFS= read -r want; do
    [[ -z "$want" ]] && continue
    rg -q -x -F -- "$want" <<< "$filtered" || missing="$missing$want"$'\n'
  done <<< "$CANONICAL_SECTIONS"
  # Order check: the sections that ARE present must appear in the same relative
  # order as CANONICAL_SECTIONS (independent of any extra, non-canonical headings).
  local expected_subseq; expected_subseq="$(printf '%s\n' "$CANONICAL_SECTIONS" | while IFS= read -r s; do rg -q -x -F -- "$s" <<< "$filtered" && printf '%s\n' "$s"; done)"
  if [[ -n "$missing" ]]; then
    fail "6 faltan $(printf '%s' "$missing" | count_lines /dev/stdin) de 12 secciones obligatorias"
    detail "$missing"
  elif [[ "$filtered" != "$expected_subseq" ]]; then
    fail "6 las 12 secciones estan presentes pero en el orden incorrecto"
    detail "encontrado:
$filtered
esperado:
$expected_subseq"
  else
    pass "6 las 12 secciones estan presentes y en orden"
  fi
}

check_mermaid() {
  local file="$1"
  printf ' -- 7. Bloques mermaid renderizan con mmdc --\n'
  python3 - "$file" "$TMP" > /dev/null <<'PY'
import sys
path, tmp = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read().split("\n")
i, n = 0, 0
while i < len(text):
    if text[i].strip() == "```mermaid":
        j = i + 1
        block = []
        while j < len(text) and text[j].strip() != "```":
            block.append(text[j])
            j += 1
        n += 1
        open(f"{tmp}/mmd_{n}.mmd", "w", encoding="utf-8").write("\n".join(block) + "\n")
        i = j
    i += 1
print(n)
PY
  # Counted with rg, not a second python heredoc: a heredoc with an odd number of
  # backtick characters, when nested inside a $(...) command substitution, breaks
  # bash 3.2's parser even though the heredoc delimiter is quoted -- verified by
  # hand (`x="$(python3 - <<'PY' ... ``` ... PY)"` throws "unexpected EOF while
  # looking for matching \`'" even though the same heredoc as a plain statement
  # parses fine).
  local n; n="$(rg -c -F -- '```mermaid' "$file" 2> /dev/null || true)"
  [[ -z "$n" ]] && n=0
  if (( n == 0 )); then
    warn "7 sin bloques mermaid en este documento; nada que renderizar (0 no es PASS)"
    return
  fi
  if [[ -z "$MMDC" ]]; then
    fail "7 mmdc no disponible (npm install -g @mermaid-js/mermaid-cli); $n bloque(s) sin validar"
    return
  fi
  local i bad="" out log rc
  for (( i = 1; i <= n; i++ )); do
    [[ -f "$TMP/mmd_$i.mmd" ]] || continue
    out="$TMP/mmd_$i.svg"; log="$TMP/mmd_$i.log"
    if [[ -n "$PUPPETEER_CFG" ]]; then
      "$MMDC" -i "$TMP/mmd_$i.mmd" -o "$out" -p "$PUPPETEER_CFG" > "$log" 2>&1
    else
      "$MMDC" -i "$TMP/mmd_$i.mmd" -o "$out" > "$log" 2>&1
    fi
    rc=$?
    if (( rc != 0 )) || [[ ! -s "$out" ]]; then
      bad="${bad}bloque $i (exit $rc): $(rg -m1 -N -i 'error|parse' "$log" 2> /dev/null | head -1)"$'\n'
    fi
  done
  if [[ -n "$bad" ]]; then
    fail "7 $(printf '%s' "$bad" | count_lines /dev/stdin) de $n bloque(s) mermaid no renderizan"
    detail "$bad"
  else
    pass "7 $n/$n bloque(s) mermaid renderizan con mmdc"
  fi
}

check_gate_rollback() {
  local file="$1"
  printf ' -- 8. Pasos [GATE] tienen fila en Rollback --\n'
  local body; body="$(body_after_frontmatter "$file")"
  local gate_n; gate_n="$(rg -c -F -- '[GATE]' <<< "$body" 2> /dev/null || true)"
  [[ -z "$gate_n" ]] && gate_n=0
  if (( gate_n == 0 )); then
    warn "8 sin pasos [GATE] en este documento; nada que verificar (0 no es PASS)"
    return
  fi
  local rb_body; rb_body="$(printf '%s\n' "$body" | awk '/^## Rollback$/{f=1;next} /^## /{f=0} f{print}')"
  # Count table rows that are neither the header ("| Step | ... |") nor the
  # "|---|---|" separator -- those are not evidence of an actual rollback step.
  local data_rows=0 line
  while IFS= read -r line; do
    [[ "$line" =~ ^\|.*\|.*\|$ ]] || continue
    [[ "$line" =~ ^\|[[:space:]]*Step[[:space:]]*\| ]] && continue
    [[ "$line" =~ ^\|[-:\ ]+\|[-:\ ]+\|.*$ ]] && continue
    data_rows=$((data_rows + 1))
  done <<< "$rb_body"
  if [[ -z "$rb_body" ]]; then
    fail "8 hay $gate_n paso(s) [GATE] pero no existe la seccion ## Rollback"
  elif (( data_rows == 0 )); then
    fail "8 hay $gate_n paso(s) [GATE] pero ## Rollback no tiene filas de datos"
  else
    pass "8 $gate_n paso(s) [GATE], ## Rollback tiene $data_rows fila(s)"
  fi
}

check_pii() {
  local file="$1"
  printf ' -- 9. Sin PII fuera de {{variables}} --\n'
  local body; body="$(body_after_frontmatter "$file")"
  local stripped; stripped="$(printf '%s\n' "$body" | sd '\{\{[^}]*\}\}' '')"
  local hits=""
  hits="$(rg -n -o --no-filename -e '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' <<< "$stripped" 2> /dev/null || true)"
  local phone_hits; phone_hits="$(rg -n -o --no-filename -w -e '[0-9]{10}' <<< "$stripped" 2> /dev/null || true)"
  # `-s` on a process-substitution fifo (count_lines <(...)) is a race and reads as
  # empty here -- verified by hand: it silently kept this check at n=0 against a
  # fixture with a known email and phone in it. Piping into /dev/stdin instead
  # (as every other check in this file already does) is not subject to that race.
  local n=0
  [[ -n "$hits" ]] && n=$((n + $(printf '%s\n' "$hits" | count_lines /dev/stdin)))
  [[ -n "$phone_hits" ]] && n=$((n + $(printf '%s\n' "$phone_hits" | count_lines /dev/stdin)))
  if (( n > 0 )); then
    fail "9 $n coincidencia(s) de PII (correo o telefono de 10 digitos) fuera de {{variables}}"
    detail "$hits"$'\n'"$phone_hits"
  else
    pass "9 sin coincidencias de PII fuera de {{variables}}"
  fi
}

check_automation_gate_consistency() {
  local fm_json="$1"
  printf ' -- 10. automation_level: autonomous-with-gate exige status: verified --\n'
  local level status
  level="$(printf '%s' "$fm_json" | jq -r '.automation_level // empty')"
  status="$(printf '%s' "$fm_json" | jq -r '.status // empty')"
  if [[ "$level" != "autonomous-with-gate" ]]; then
    warn "10 automation_level='$level', no aplica esta regla (0 no es PASS)"
    return
  fi
  if [[ "$status" == "verified" ]]; then
    pass "10 automation_level=autonomous-with-gate con status=verified"
  else
    fail "10 automation_level=autonomous-with-gate exige status=verified, tiene status='$status'"
  fi
}

check_language() {
  local file="$1"
  printf ' -- 11. Sin espanolismos/regionalismos en la plantilla en espanol (Communication) --\n'
  local body; body="$(body_after_frontmatter "$file")"
  local comm; comm="$(printf '%s\n' "$body" | awk '/^## Communication$/{f=1;next} /^## /{f=0} f{print}')"
  if [[ -z "$comm" ]]; then
    warn "11 sin seccion ## Communication; nada que revisar (0 no es PASS)"
    return
  fi
  local hits; hits="$(rg -n -w -i -o --no-filename -e "$LANG_FORBIDDEN" <<< "$comm" 2> /dev/null || true)"
  if [[ -n "$hits" ]]; then
    fail "11 $(printf '%s\n' "$hits" | count_lines /dev/stdin) coincidencia(s) de espanolismos/regionalismos"
    detail "$hits"
  else
    pass "11 sin espanolismos/regionalismos en ## Communication"
  fi
}

check_no_eval() {
  local file="$1"
  printf ' -- 12. Nunca el identificador eval seguido de parentesis --\n'
  if rg -q -F -- 'eval(' "$file" 2> /dev/null; then
    fail "12 se encontro 'eval(' en el archivo"
    detail "$(rg -n -F -- 'eval(' "$file" 2> /dev/null)"
  else
    pass "12 sin 'eval(' en el archivo"
  fi
}

# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

collect_default_targets() {
  [[ -d "$VAULT_ROOT/Runbooks" ]] || return 0
  local f
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    [[ "$f" == *"/Templates/"* ]] && continue
    rg -q -F -- 'type: runbook' "$f" 2> /dev/null && printf '%s\n' "$f"
  done < <(fd -H -I -t f -e md . "$VAULT_ROOT/Runbooks" 2> /dev/null | sort)
}

TARGETS=()
if (( $# > 0 )); then
  TARGETS=("$@")
else
  while IFS= read -r f; do TARGETS+=("$f"); done < <(collect_default_targets)
fi

if (( ${#TARGETS[@]} == 0 )); then
  printf 'validate-runbook.sh: no hay archivos que validar\n' >&2
  exit 1
fi

build_vault_id_index

FILE_FAIL_N=0
for f in "${TARGETS[@]}"; do
  printf '\n=== %s ===\n' "$f"
  if [[ ! -f "$f" ]]; then
    fail "0 el archivo no existe"
    FILE_FAIL_N=$((FILE_FAIL_N + 1))
    continue
  fi
  before_fail=$FAIL_N
  fm_json="$(frontmatter_json "$f")"
  check_frontmatter_fields "$f" "$fm_json"
  check_enums "$fm_json"
  check_id_unique_and_aligned "$f" "$fm_json"
  check_symptoms_and_jira_refs "$fm_json"
  check_tools "$fm_json"
  check_sections "$f"
  check_mermaid "$f"
  check_gate_rollback "$f"
  check_pii "$f"
  check_automation_gate_consistency "$fm_json"
  check_language "$f"
  check_no_eval "$f"
  if (( FAIL_N > before_fail )); then
    FILE_FAIL_N=$((FILE_FAIL_N + 1))
  fi
done

printf '\n== Resumen ==\n'
printf 'Archivos: %d  con FAIL: %d\n' "${#TARGETS[@]}" "$FILE_FAIL_N"
printf 'PASS: %d  FAIL: %d  WARN: %d\n' "$PASS_N" "$FAIL_N" "$WARN_N"
if (( FAIL_N > 0 )); then
  printf 'Resultado: CORRECCIONES REQUERIDAS (exit 1)\n'
  exit 1
fi
printf 'Resultado: TODO PASS (exit 0)\n'
exit 0
