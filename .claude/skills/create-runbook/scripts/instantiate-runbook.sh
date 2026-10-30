#!/usr/bin/env bash
# instantiate-runbook.sh — fills ai-platform-plan/05-runbooks/plantilla-runbook.md
# section 3's literal skeleton for one new draft runbook and writes it OUTSIDE the
# vault. It never touches the vault clone: guard_dest_outside_vault (lib/guards.sh)
# refuses to write when --out-dir resolves inside the real vault, regardless of any
# MEMORY_VAULT_ROOT override the caller sets.
#
# Usage:
#   instantiate-runbook.sh --id RB-NNN --pattern <pattern> --out-dir <dir> \
#     [--title "English title"] [--service <slug>] [--severity P1|P2|P3|P4] \
#     [--category <enum>] [--country CO,MX,...] [--partner telvia,comet,none,...] \
#     [--jira-refs EXMP-1,EXMP-2] [--fixture <ticket.json>]
#
# --fixture points at a JSON file shaped like tests/fixtures/*.json (key, summary,
# country, symptoms[]) and only fills symptoms/jira_refs/last_occurrence/country
# when the matching --flag was not given explicitly (flags always win).
#
# Prints the absolute path of the written file on stdout and nothing else there;
# progress and the list of {{...}} placeholders still pending a human fill go to
# stderr. Exit 0 on success. Distinct non-zero codes on failure (see `case` below
# for the exact one) so a caller can tell "bad input" from "pattern not on file"
# from "destination unsafe" without grepping text.
#
# Bash-3.2-safe: no arrays, no ${var,,}, no associative arrays. Tools required:
# awk, sd, jq, yq, date. Compatible with macOS's shipped bash 3.2 and bash 5.
set -u

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SELF_DIR/.." && pwd)"
# shellcheck source=lib/guards.sh
source "$SELF_DIR/lib/guards.sh"

AI_ARCH_ROOT="${AI_ARCH_ROOT:-$(runbook_arch_root || true)}"
PLANTILLA="${PLANTILLA:-$AI_ARCH_ROOT/ai-platform-plan/05-runbooks/plantilla-runbook.md}"
PATTERNS_DIR="$SKILL_DIR/patterns"

ID=""
PATTERN=""
OUT_DIR=""
TITLE=""
SERVICE_OVERRIDE=""
SEVERITY_OVERRIDE=""
CATEGORY_OVERRIDE=""
COUNTRY_OVERRIDE=""
PARTNER_OVERRIDE=""
JIRA_REFS_OVERRIDE=""
FIXTURE=""

usage() {
  printf 'Usage: instantiate-runbook.sh --id RB-NNN --pattern <pattern> --out-dir <dir> [options]\n' >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --id) ID="$2"; shift 2 ;;
    --pattern) PATTERN="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --title) TITLE="$2"; shift 2 ;;
    --service) SERVICE_OVERRIDE="$2"; shift 2 ;;
    --severity) SEVERITY_OVERRIDE="$2"; shift 2 ;;
    --category) CATEGORY_OVERRIDE="$2"; shift 2 ;;
    --country) COUNTRY_OVERRIDE="$2"; shift 2 ;;
    --partner) PARTNER_OVERRIDE="$2"; shift 2 ;;
    --jira-refs) JIRA_REFS_OVERRIDE="$2"; shift 2 ;;
    --fixture) FIXTURE="$2"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'instantiate-runbook.sh: unknown argument "%s"\n' "$1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "$ID" || -z "$PATTERN" || -z "$OUT_DIR" ]]; then
  printf 'instantiate-runbook.sh: --id, --pattern and --out-dir are required\n' >&2
  usage
  exit 2
fi

case "$ID" in
  RB-[0-9][0-9][0-9]) : ;;
  *)
    printf 'instantiate-runbook.sh: --id "%s" does not match RB-NNN\n' "$ID" >&2
    exit 2
    ;;
esac

guard_pattern_known "$PATTERN" "$PATTERNS_DIR" || exit $?

if [[ ! -f "$PLANTILLA" ]]; then
  printf 'instantiate-runbook.sh: plantilla not found at %s (set AI_ARCH_ROOT)\n' "$PLANTILLA" >&2
  exit 6
fi

# The guard runs BEFORE anything touches disk at --out-dir: guard_dest_outside_vault
# resolves a not-yet-existing path just fine (it falls back to the path as given
# when `cd` fails), so there is no need to `mkdir` first to make the check work --
# and doing `mkdir -p` first would create the directory inside the real vault as a
# side effect even on the path that is supposed to refuse and exit before any write.
guard_dest_outside_vault "$OUT_DIR" "$REAL_VAULT_ROOT_DEFAULT" || exit $?
mkdir -p "$OUT_DIR" 2> /dev/null

# --------------------------------------------------------------------------- #
# Pattern defaults (frontmatter of patterns/<pattern>.md)
# --------------------------------------------------------------------------- #
pat_json="$(frontmatter_json_of "$PATTERNS_DIR/$PATTERN.md")"
if [[ -z "$pat_json" ]]; then
  printf 'instantiate-runbook.sh: patterns/%s.md frontmatter does not parse\n' "$PATTERN" >&2
  exit 6
fi

pat_get() { printf '%s' "$pat_json" | jq -r --arg f "$1" '.[$f] // empty'; }
pat_get_csv() { printf '%s' "$pat_json" | jq -r --arg f "$1" '(.[$f] // []) | join(", ")'; }

CATEGORY="${CATEGORY_OVERRIDE:-$(pat_get category)}"
SERVICE="${SERVICE_OVERRIDE:-$(pat_get service)}"
SEVERITY="${SEVERITY_OVERRIDE:-$(pat_get severity)}"
TOOLS_CSV="$(pat_get_csv tools)"
EVIDENCE_CSV="$(pat_get_csv evidence_required)"

if [[ "$PATTERN" == "other" && ( -z "$CATEGORY" || -z "$SERVICE" ) ]]; then
  printf 'instantiate-runbook.sh: pattern "other" has no default category/service; pass --category and --service explicitly\n' >&2
  exit 2
fi
if [[ -z "$CATEGORY" || -z "$SERVICE" || -z "$SEVERITY" ]]; then
  printf 'instantiate-runbook.sh: could not resolve category/service/severity for pattern "%s"\n' "$PATTERN" >&2
  exit 6
fi

# --------------------------------------------------------------------------- #
# Fixture (optional): a recorded, already-sanitized ticket JSON. See
# tests/fixtures/README.md for the shape and the sanitization rule.
# --------------------------------------------------------------------------- #
FIX_SUMMARY="" FIX_COUNTRY="" FIX_KEY="" FIX_CREATED="" FIX_ISSUETYPE="Issue productivo"
SYMPTOM_1="" SYMPTOM_2=""
if [[ -n "$FIXTURE" ]]; then
  if [[ ! -f "$FIXTURE" ]]; then
    printf 'instantiate-runbook.sh: --fixture "%s" not found\n' "$FIXTURE" >&2
    exit 2
  fi
  FIX_SUMMARY="$(jq -r '.summary // empty' "$FIXTURE")"
  FIX_COUNTRY="$(jq -r '.country_enum // empty' "$FIXTURE")"
  FIX_KEY="$(jq -r '.key // empty' "$FIXTURE")"
  FIX_CREATED="$(jq -r '.created // empty' "$FIXTURE")"
  FIX_ISSUETYPE="$(jq -r '.issue_type // "Issue productivo"' "$FIXTURE")"
  SYMPTOM_1="$(jq -r '.symptoms[0] // empty' "$FIXTURE")"
  SYMPTOM_2="$(jq -r '.symptoms[1] // empty' "$FIXTURE")"
fi

TITLE="${TITLE:-${FIX_SUMMARY:-Draft runbook $ID (pending title)}}"
COUNTRY="${COUNTRY_OVERRIDE:-${FIX_COUNTRY:-ALL}}"
PARTNER="${PARTNER_OVERRIDE:-none}"
JIRA_REFS="${JIRA_REFS_OVERRIDE:-$FIX_KEY}"
LAST_OCCURRENCE="${FIX_CREATED:-$(date +%Y-%m-%d)}"
TODAY="$(date +%Y-%m-%d)"
OWNER="${SERVICE}-team"
AUTOMATION_LEVEL="manual"
[[ -n "$SYMPTOM_1" ]] || SYMPTOM_1="TODO: pending human-confirmed symptom (fill from the ticket before status: verified)"
[[ -n "$SYMPTOM_2" ]] || SYMPTOM_2="TODO: pending second human-confirmed symptom"

# --------------------------------------------------------------------------- #
# Extract the literal skeleton (plantilla-runbook.md section 3) and substitute
# every FRONTMATTER placeholder with a real, YAML-safe value. Body placeholders
# ({{quick_step}}, {{mutating_step}}, {{context}}, ...) are left as-is on
# purpose: they need the human-confirmed diagnosis this script does not have,
# and validate-runbook.sh's check 9 explicitly expects {{variables}} to remain
# in the body of a draft.
#
# Substitution is mandatory, not cosmetic, for the fields below: an unquoted
# "{{token}}" inside a YAML flow list ("[{{x}}]") or a bare scalar ("id: {{id}}")
# does not parse as YAML at all -- verified by hand with `yq` against the raw
# skeleton, which fails at the first such line. Quoted placeholders ("title:
# \"{{title}}\"") parse fine either way, but are substituted too whenever real
# data is available, so the draft is not sloppier than it has to be.
#
# This runs as ONE python3 pass over the whole file with plain str.replace
# (zero escape re-interpretation) instead of one `sd` invocation per token.
# `sd`'s replacement string has its OWN escape syntax, so chaining a shell-side
# escape (to protect an embedded '"' in a symptom, e.g. 'User sees: "Incorrect
# username or password"') through a SECOND sd call re-parses and silently
# drops the backslash that the first escape put there -- reproduced by hand
# with the EXMP-1234 fixture, whose symptom text quotes the exact
# user-visible message and broke the frontmatter's YAML on the first attempt
# (see run-instantiate-canaries.sh, "symptom with an embedded double quote").
# Passing the mapping as JSON (built with `jq -n`, which escapes correctly)
# and letting Python's json module decode it back to a real string sidesteps
# shell quoting entirely; only "title", "symptom_1" and "symptom_2" can carry
# free text from a ticket, so only those get the YAML double-quote escape
# before substitution -- every other token here is our own enum-safe value.
# --------------------------------------------------------------------------- #
TMP_SKELETON="$(mktemp "${TMPDIR:-/tmp}/instantiate-runbook.XXXXXX")"
TMP_MAPPING="$(mktemp "${TMPDIR:-/tmp}/instantiate-runbook-map.XXXXXX")"
trap 'rm -f "$TMP_SKELETON" "$TMP_MAPPING"' EXIT

awk '/^````markdown$/{f=1;next} /^````$/{if(f){exit}} f{print}' "$PLANTILLA" > "$TMP_SKELETON"
if [[ ! -s "$TMP_SKELETON" ]]; then
  printf 'instantiate-runbook.sh: could not extract the literal skeleton from %s (section 3 markers not found)\n' "$PLANTILLA" >&2
  exit 6
fi

jq -n \
  --arg id "$ID" --arg title "$TITLE" --arg service "$SERVICE" --arg severity "$SEVERITY" \
  --arg category "$CATEGORY" --arg pattern "$PATTERN" --arg owner "$OWNER" \
  --arg automation_level "$AUTOMATION_LEVEL" --arg today "$TODAY" \
  --arg last_occurrence "$LAST_OCCURRENCE" --arg symptom_1 "$SYMPTOM_1" --arg symptom_2 "$SYMPTOM_2" \
  --arg jira_issuetype "$FIX_ISSUETYPE" --arg affected_services "$SERVICE" \
  --arg jira_refs "$JIRA_REFS" --arg country "$COUNTRY" --arg partner "$PARTNER" \
  --arg tools "$TOOLS_CSV" --arg evidence_required "$EVIDENCE_CSV" \
  '{$id, $title, $service, $severity, $category, $pattern, $owner, $automation_level,
    $today, $last_occurrence, $symptom_1, $symptom_2, $jira_issuetype, $affected_services,
    affected_endpoints: "", affected_tables: "", affected_databases: "",
    $jira_refs, $country, $partner, $tools, $evidence_required}' > "$TMP_MAPPING"

python3 - "$TMP_MAPPING" "$TMP_SKELETON" <<'PY'
import json, sys
mapping_path, skeleton_path = sys.argv[1], sys.argv[2]
mapping = json.load(open(mapping_path, encoding="utf-8"))
text = open(skeleton_path, encoding="utf-8").read()
# Only these three can carry free text copied from a ticket; every other key
# in `mapping` is our own enum/id/date value and never contains a quote.
quoted_fields = {"title", "symptom_1", "symptom_2"}
for key, value in mapping.items():
    if key in quoted_fields:
        value = value.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("{{" + key + "}}", value)
open(skeleton_path, "w", encoding="utf-8").write(text)
PY
[[ $? -eq 0 ]] || { printf 'instantiate-runbook.sh: the substitution pass failed\n' >&2; exit 6; }

# --------------------------------------------------------------------------- #
# Write to <out-dir>/<id> <title>.md — matches the vault's own naming
# convention ("RB-001 Telvia Line Not Recognized On Register.md") and satisfies
# validate-runbook.sh check 3 (filename must start with "<id> "). The filename
# uses the RAW title (pre-YAML-escape), with '/' and '\' swapped for '-' since
# those are path separators; a title with an embedded '"' still works as a
# filename on macOS/Linux, so it is left untouched here.
# --------------------------------------------------------------------------- #
SAFE_TITLE="$(printf '%s' "$TITLE" | sd -- '[/\\]' '-')"
OUT_FILE="$OUT_DIR/$ID $SAFE_TITLE.md"
cp "$TMP_SKELETON" "$OUT_FILE"

remaining="$(rg -o -- '\{\{[a-zA-Z_]+\}\}' "$OUT_FILE" 2> /dev/null | wc -l | tr -d ' ')"
printf 'instantiate-runbook.sh: wrote %s\n' "$OUT_FILE" >&2
printf 'instantiate-runbook.sh: %s body placeholder(s) still pending a human fill before status: verified\n' "$remaining" >&2
printf '%s\n' "$OUT_FILE"
exit 0
