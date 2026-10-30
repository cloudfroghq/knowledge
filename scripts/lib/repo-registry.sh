#!/usr/bin/env bash
# repo-registry.sh — shell access to the repository registry.
#
# Source it, never execute it:
#
#   . "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/repo-registry.sh"
#
# There is one registry and one implementation of its resolution rules:
# src/knowledge/repos.py, which this file calls. Reimplementing the lookup in
# bash would give the platform two answers to "where is repository X" that
# drift apart the first time an override is added to one of them.
#
# What is declared where:
#   <vault>/Contexts/config.json -> repos
#                             what each repository IS. Versioned with the vault.
#   ~/.config/knowledge/<slug>/repos.local.json
#                             where the roots ARE on this machine; written by
#                             `cloudfrog-knowledge init`.
#   KNOWLEDGE_<ROOT>_ROOT     override for a whole root. CD_<ROOT>_ROOT and the
#                             root's declared 'env' still work, deprecated.
#   KNOWLEDGE_REPO_<ID>       override for one checkout, e.g. KNOWLEDGE_REPO_EXAMPLE_SERVICE.
#                             CD_REPO_<ID> still works, deprecated.
#
# Exported:
#   REPO_REGISTRY_ROOT        the knowledge checkout holding the registry.
#   REPO_REGISTRY_PYTHON      the interpreter used to read it.
#   REPO_REGISTRY_REASON      why the registry is unusable, when it is.
#
# Functions:
#   repo_registry_ready            0 when the registry can be read, 1 otherwise.
#   repo_path <id>                 print the resolved path, or nothing.
#                                  Exit 0 resolved, 3 not resolved, 2 unknown id.
#   repo_status <id>               print resolved | missing | unconfigured.
#   repo_context <id>               print the Contexts/<slug> package of the checkout,
#                                  or nothing when it declares context: null.
#   repo_install_mode <id>          print the declared install_mode
#                                  ('link'/'copy'), or nothing when the
#                                  checkout has no context to materialize.
#                                  Declarative: does not need the checkout to
#                                  be resolved on this machine.
#   repo_ids                       print every declared id, one per line.
#   repo_registry_report           print the resolution table.
#   repo_require <id> [id...]      SKIP (exit 77) naming the override to export
#                                  when a checkout is unconfigured; FAIL (exit 1)
#                                  when it resolved and is not on disk.
#
# The three-state contract is the point. "unconfigured" means nothing was
# inspected and nothing may be concluded; "missing" means this machine said
# where to look and the directory is not there. Neither is a pass, and a caller
# that collapses them reports green for a tree it never opened.
#
# Compatible with the bash 3.2 macOS ships: no associative arrays, no mapfile.

REPO_REGISTRY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
export REPO_REGISTRY_ROOT

REPO_REGISTRY_SKIP_EXIT="${ROOTS_SKIP_EXIT:-77}"
export REPO_REGISTRY_SKIP_EXIT

REPO_REGISTRY_REASON=""

# Prefer the project virtualenv when it is there, but never require it: the
# registry module imports nothing outside the standard library precisely so a
# script can read it on a machine that has not run `uv sync`.
if [ -n "${CD_REGISTRY_PYTHON:-}" ] && [ -x "${CD_REGISTRY_PYTHON}" ]; then
  REPO_REGISTRY_PYTHON="$CD_REGISTRY_PYTHON"
elif [ -x "$REPO_REGISTRY_ROOT/.venv/bin/python" ]; then
  REPO_REGISTRY_PYTHON="$REPO_REGISTRY_ROOT/.venv/bin/python"
elif command -v python3 > /dev/null 2>&1; then
  REPO_REGISTRY_PYTHON="$(command -v python3)"
else
  REPO_REGISTRY_PYTHON=""
  REPO_REGISTRY_REASON="no hay python3 en PATH ni .venv/bin/python en $REPO_REGISTRY_ROOT"
fi
export REPO_REGISTRY_PYTHON

if [ -n "$REPO_REGISTRY_PYTHON" ] && [ ! -f "$REPO_REGISTRY_ROOT/src/knowledge/repos.py" ]; then
  REPO_REGISTRY_REASON="$REPO_REGISTRY_ROOT/src/knowledge/repos.py no existe"
fi
export REPO_REGISTRY_REASON

__repo_registry_run() {
  if [ -n "$REPO_REGISTRY_REASON" ]; then
    printf 'ERROR: el registro de repositorios no se puede leer (%s)\n' "$REPO_REGISTRY_REASON" >&2
    return 2
  fi
  PYTHONPATH="$REPO_REGISTRY_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$REPO_REGISTRY_PYTHON" -m knowledge.repos "$@"
}

# repo_registry_ready — 0 when the registry is readable, 1 otherwise. The
# reason, when there is one, is in REPO_REGISTRY_REASON.
repo_registry_ready() {
  [ -z "$REPO_REGISTRY_REASON" ] || return 1
  __repo_registry_run --ids > /dev/null 2>&1
}

# repo_path ID — print the resolved directory, or nothing. Exit code carries
# the verdict so `path="$(repo_path x)" || handle` works.
repo_path() {
  __repo_registry_run --path "$1"
}

# repo_status ID — resolved | missing | unconfigured.
repo_status() {
  __repo_registry_run --status "$1"
}

# repo_context ID — print the Contexts/<slug> package for this checkout, or nothing
# when it declares context: null (e.g. a vendored package).
repo_context() {
  __repo_registry_run --context "$1"
}

# repo_install_mode ID — print the declared install_mode ('link' or 'copy'),
# or nothing when the checkout has no context to materialize. This is a
# declarative fact from config.json, so unlike repo_path it does not require
# the checkout to be resolved on this machine.
repo_install_mode() {
  __repo_registry_run --install-mode "$1"
}

repo_ids() {
  __repo_registry_run --ids
}

repo_registry_report() {
  __repo_registry_run --report "$@"
}

# repo_require ID [ID...] — stop the caller unless every checkout is on disk.
#
# The two failures exit differently on purpose. An unconfigured checkout is an
# environment gap: the run is neither green nor red because nothing was
# verified, so it SKIPs with 77 and names the variable to export. A checkout
# this machine located and does not have is a real gap, so it FAILs with 1.
repo_require() {
  local id status path
  for id in "$@"; do
    status="$(repo_status "$id")" || {
      printf 'ERROR: %s no está declarado en el registro\n' "$id" >&2
      exit 2
    }
    case "$status" in
      resolved) continue ;;
      unconfigured)
        printf 'SKIP: %s no está configurado en esta máquina; declara su raíz en repos.local.json o exporta %s\n' \
          "$id" "$(__repo_registry_var "$id")" >&2
        exit "$REPO_REGISTRY_SKIP_EXIT"
        ;;
      missing)
        path="$(__repo_registry_run --json 2> /dev/null | __repo_registry_field "$id")"
        printf 'FAIL: %s está configurado pero no está en disco%s\n' "$id" "${path:+ ($path)}" >&2
        exit 1
        ;;
      *)
        printf 'ERROR: estado desconocido %s para %s; el registro NO fue interpretado\n' "$status" "$id" >&2
        exit 2
        ;;
    esac
  done
}

# Name of the per-checkout override variable, mirroring repos.repo_env_var.
# CD_REPO_<ID> still works as a deprecated alias.
__repo_registry_var() {
  printf 'KNOWLEDGE_REPO_%s' "$(printf '%s' "$1" | tr 'a-z.-' 'A-Z__')"
}

# Pull one entry's resolved path out of the JSON report, without needing jq.
__repo_registry_field() {
  "$REPO_REGISTRY_PYTHON" -c '
import json, sys
wanted = sys.argv[1]
try:
    payload = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for entry in payload.get("entries", []):
    if entry.get("id") == wanted:
        print(entry.get("path") or "")
        break
' "$1"
}
