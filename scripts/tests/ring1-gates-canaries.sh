#!/usr/bin/env bash
# ring1-gates-canaries.sh — canary battery for the ring-1 gates of this repository.
#
# A gate nobody has watched fail is a claim, not a gate. Every case below either
# breaks something on purpose and demands the exact failure, or leaves the tree
# alone and demands a pass. Where a gate has an exit code of its own, the code is
# pinned with equality — "non-zero" hides a usage error behind a real failure.
#
# The battery never touches the working tree: each case runs against its own copy
# under a temporary directory that the trap removes. Every fixture value is
# synthetic. The scanner-suppression cases assemble the marker they inject at
# runtime out of two harmless fragments, so this file never contains the literal
# token — otherwise the battery would silence rules in the very tree it audits.
#
# Written for the bash 3.2 that macOS ships. Run it under both interpreters:
#   /bin/bash scripts/tests/ring1-gates-canaries.sh
#   /opt/homebrew/bin/bash scripts/tests/ring1-gates-canaries.sh
#
# The Semgrep cases need Docker and are opt-in, because they pull a pinned image:
#   /bin/bash scripts/tests/ring1-gates-canaries.sh --with-semgrep

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON="$REPO_ROOT/.venv/bin/python"
LINT_IMPORTS="$REPO_ROOT/.venv/bin/lint-imports"
SEMGREP_IMAGE="semgrep/semgrep:1.175.0"

WITH_SEMGREP=0
if [ "${1:-}" = "--with-semgrep" ]; then
  WITH_SEMGREP=1
fi

[ -x "$PYTHON" ] || {
  printf 'error: no virtualenv interpreter at %s — run `uv sync --all-groups` first\n' "$PYTHON" >&2
  exit 2
}

[ -x "$LINT_IMPORTS" ] || {
  printf 'error: no lint-imports at %s — run `uv sync --all-groups` first\n' "$LINT_IMPORTS" >&2
  exit 2
}

PASS_N=0
FAIL_N=0
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ring1-canaries.XXXXXX")"
trap 'rm -rf "$ROOT"' EXIT

pass() { PASS_N=$((PASS_N + 1)); printf '[PASS] %s\n' "$1"; }
fail() { FAIL_N=$((FAIL_N + 1)); printf '[FAIL] %s\n' "$1"; }

# make_tree <name>
#   Prints the path to a fresh, unmutated copy of everything the gates read.
make_tree() {
  local name="$1"
  local dst="$ROOT/$name"
  local part
  mkdir -p "$dst"
  for part in src tests scripts pyproject.toml .github .importlinter; do
    cp -R "$REPO_ROOT/$part" "$dst/"
  done
  printf '%s\n' "$dst"
}

# run_pytest <tree> <logfile> <selector> [selector...]
run_pytest() {
  local tree="$1" log="$2"
  shift 2
  ( cd "$tree" && "$PYTHON" -m pytest -q -p no:cacheprovider "$@" ) > "$log" 2>&1
  return $?
}

# run_lint_imports <tree> <logfile>
#   Runs the exact command the "Layered architecture: import-linter (ADR-022)"
#   workflow job runs. PYTHONPATH is forced to the tree's own src/: lint-imports
#   is the console script from the shared .venv at REPO_ROOT, and that venv has
#   knowledge-mcp installed *editable*, pointing at REPO_ROOT/src — without the
#   override every case here would silently lint the real repository instead of
#   the mutated copy, and every canary would look green no matter what it broke.
run_lint_imports() {
  local tree="$1" log="$2"
  ( cd "$tree" && PYTHONPATH="$tree/src" "$LINT_IMPORTS" --config .importlinter --no-cache --no-logo ) > "$log" 2>&1
  return $?
}

# expect_exit <label> <expected> <actual>
expect_exit() {
  local label="$1" expected="$2" actual="$3"
  if [ "$actual" -eq "$expected" ]; then
    pass "$label (exit $actual)"
  else
    fail "$label — expected exit $expected, got $actual"
  fi
}

# expect_in_log <label> <logfile> <needle>
expect_in_log() {
  local label="$1" log="$2" needle="$3"
  if grep -q -- "$needle" "$log"; then
    pass "$label"
  else
    fail "$label — log does not mention: $needle"
    sed -n '1,20p' "$log" | sed 's/^/       | /'
  fi
}

# expect_not_in_log <label> <logfile> <needle>
expect_not_in_log() {
  local label="$1" log="$2" needle="$3"
  if grep -q -- "$needle" "$log"; then
    fail "$label — log unexpectedly mentions: $needle"
  else
    pass "$label"
  fi
}

# expect_failed_count <label> <logfile> <expected>
#   Pins how many tests a mutation brings down. "At least one failed" is what
#   lets a report claim three guards fired when only two could.
expect_failed_count() {
  local label="$1" log="$2" expected="$3" actual
  actual="$(grep -c '^FAILED ' "$log" | tr -d ' ')"
  if [ "$actual" = "$expected" ]; then
    pass "$label ($actual failing test(s))"
  else
    fail "$label — expected exactly $expected failing test(s), got $actual"
    grep '^FAILED ' "$log" | sed 's/^/       | /'
  fi
}

# edit <tree> <python-program>
#   Applies one mutation. A canary whose mutation silently did nothing is a
#   canary that always passes, so a missing anchor aborts the whole battery.
edit() {
  local tree="$1" prog="$2"
  ( cd "$tree" && "$PYTHON" - <<PYEOF
from pathlib import Path
import sys

# Assembled, never written literally: this string is a scanner suppression and
# would apply to this repository's own source if it appeared in a scanned file.
MARK = "# " + "nose" + "mgrep"

def sub(path, old, new, count=1):
    p = Path(path)
    s = p.read_text()
    if old not in s:
        sys.exit("canary anchor not found in %s: %r" % (path, old[:70]))
    p.write_text(s.replace(old, new, count))

def append(path, text):
    p = Path(path)
    p.write_text(p.read_text() + text)

$prog
PYEOF
  ) || {
    printf '[FATAL] canary mutation failed to apply in %s\n' "$tree" >&2
    exit 2
  }
}

printf '== ring-1 gate canaries ==\n\n'

# ---------------------------------------------------------------------------
# A. Response-freeze guard: it must read the syntax tree, not the raw text.
# ---------------------------------------------------------------------------

printf -- '-- A. response freeze --\n'

T="$(make_tree a0)"
run_pytest "$T" "$ROOT/a0.log" tests/test_tool_registry.py
expect_exit "A0 unmutated tree: every registry guard passes" 0 $?

T="$(make_tree a1)"
edit "$T" 'sub("tests/test_response_schemas.py",
    "    results = asyncio.run(indexed_server.search_by_metadata(type=\"service\"))",
    "    results = []")'
run_pytest "$T" "$ROOT/a1.log" tests/test_tool_registry.py
expect_exit "A1 a tool loses its freeze: guard fires" 1 $?
expect_in_log "A1 names the unfrozen tool" "$ROOT/a1.log" "search_by_metadata"

# The hole the previous text match left open: a commented-out call still holds
# the substring the old check searched for, so it counted as coverage.
T="$(make_tree a2)"
edit "$T" 'sub("tests/test_response_schemas.py",
    "    results = asyncio.run(indexed_server.search_by_metadata(type=\"service\"))",
    "    # results = asyncio.run(indexed_server.search_by_metadata(type=\"service\"))\n    results = []")'
run_pytest "$T" "$ROOT/a2.log" tests/test_tool_registry.py
expect_exit "A2 freeze call commented out: guard fires (a text match would not)" 1 $?
expect_in_log "A2 names the unfrozen tool" "$ROOT/a2.log" "search_by_metadata"

# The same hole from the other side: the call text living inside a docstring.
T="$(make_tree a3)"
edit "$T" 'sub("tests/test_response_schemas.py",
    "    results = asyncio.run(indexed_server.search_by_metadata(type=\"service\"))",
    "    \"\"\"Frozen by indexed_server.search_by_metadata(type=service).\"\"\"\n    results = []")'
run_pytest "$T" "$ROOT/a3.log" tests/test_tool_registry.py
expect_exit "A3 freeze call only inside a docstring: guard fires" 1 $?
expect_in_log "A3 names the unfrozen tool" "$ROOT/a3.log" "search_by_metadata"

# The other direction of the contract: a freeze that outlived its tool.
T="$(make_tree a4)"
edit "$T" 'append("tests/test_response_schemas.py",
    "\n\ndef test_orphan(indexed_server) -> None:\n"
    "    asyncio.run(indexed_server.tool_that_no_longer_exists())\n")'
run_pytest "$T" "$ROOT/a4.log" tests/test_tool_registry.py
expect_exit "A4 freeze for a tool nobody registers: leftover guard fires" 1 $?
expect_in_log "A4 names the orphaned freeze" "$ROOT/a4.log" "tool_that_no_longer_exists"

# ---------------------------------------------------------------------------
# B. Registry guards: which of them a given mutation can actually bring down.
# ---------------------------------------------------------------------------

printf -- '\n-- B. registry surface --\n'

T="$(make_tree b1)"
edit "$T" 'append("src/knowledge/server.py",
    "\n\n@mcp.tool()\ndef canary_new_tool(payload: str = \"\") -> dict:\n"
    "    \"\"\"Canary: declared and registered, deliberately unfrozen.\"\"\"\n"
    "    return {\"payload\": payload}\n")'
run_pytest "$T" "$ROOT/b1.log" tests/test_tool_registry.py
expect_exit "B1 one more registered tool: guards fire" 1 $?
# Exactly two, not three. The declared-vs-registered guard counts decorator
# lines against the size of the registry, and this mutation moves both by one,
# so it cannot fire here.
expect_failed_count "B1 exactly two guards can fire" "$ROOT/b1.log" 2
expect_in_log "B1 the expected-set guard fires" "$ROOT/b1.log" "test_the_registry_holds_the_expected_tool_set"
expect_in_log "B1 the freeze guard fires" "$ROOT/b1.log" "test_every_registered_tool_has_a_response_freeze"
expect_not_in_log "B1 the declared-count guard cannot fire on this mutation" "$ROOT/b1.log" \
  "test_every_declared_tool_reaches_the_registry"

# The mutation the declared-vs-registered guard exists for, which had no canary:
# a duplicated decorator — a bad merge, a copy-pasted tool — declares a tool the
# registry silently drops.
T="$(make_tree b2)"
edit "$T" 'sub("src/knowledge/server.py",
    "@mcp.tool()\n@_offloaded\ndef search(",
    "@mcp.tool()\n@mcp.tool()\n@_offloaded\ndef search(")'
run_pytest "$T" "$ROOT/b2.log" tests/test_tool_registry.py
expect_exit "B2 a declared tool the registry drops: guard fires" 1 $?
expect_failed_count "B2 exactly the declared-count guard" "$ROOT/b2.log" 1
expect_in_log "B2 names the declared-count guard" "$ROOT/b2.log" \
  "test_every_declared_tool_reaches_the_registry"

T="$(make_tree b3)"
edit "$T" 'sub("tests/test_response_schemas.py",
    "here every registered tool gets its response frozen",
    "here each of the fifteen tools gets its response frozen")'
run_pytest "$T" "$ROOT/b3.log" tests/test_tool_registry.py
expect_exit "B3 a tool count back in a contract docstring: guard fires" 1 $?
expect_in_log "B3 quotes the offending count" "$ROOT/b3.log" "fifteen tools"

# Negative control: the guard is scoped to the contract docstrings, so a
# per-area module stating how many tools *it* covers stays legal.
T="$(make_tree b4)"
edit "$T" 'sub("tests/test_curation_tools.py", "\"\"\"", "\"\"\"Covers the seven tools nobody else touches.\n\n", 1)'
run_pytest "$T" "$ROOT/b4.log" tests/test_tool_registry.py
expect_exit "B4 a count in a non-contract docstring is not flagged" 0 $?

# ---------------------------------------------------------------------------
# D. Scanner suppressions: the SAST zero has to stay a declared zero.
# ---------------------------------------------------------------------------

printf -- '\n-- D. suppressions --\n'

T="$(make_tree d0)"
run_pytest "$T" "$ROOT/d0.log" tests/test_suppressions.py
expect_exit "D0 unmutated tree: the suppression inventory matches the tree" 0 $?

T="$(make_tree d1)"
edit "$T" 'append("src/knowledge/indexer.py",
    "\n" + MARK + ": python.lang.security.audit.canary-rule.canary-rule\n")'
run_pytest "$T" "$ROOT/d1.log" tests/test_suppressions.py
expect_exit "D1 an undeclared suppression: guard fires" 1 $?
expect_in_log "D1 names the rule it would have silenced" "$ROOT/d1.log" "canary-rule"

T="$(make_tree d2)"
edit "$T" 'append("src/knowledge/indexer.py", "\n" + MARK + "\n")'
run_pytest "$T" "$ROOT/d2.log" tests/test_suppressions.py
expect_exit "D2 a blanket suppression with no rule id: guard fires" 1 $?
expect_in_log "D2 names the blanket guard" "$ROOT/d2.log" "test_no_suppression_is_blanket"

T="$(make_tree d3)"
edit "$T" 'sub("src/knowledge/confluence_sync.py",
    "            " + MARK + ": python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected\n",
    "")'
run_pytest "$T" "$ROOT/d3.log" tests/test_suppressions.py
expect_exit "D3 the inventory describes a suppression that is gone: guard fires" 1 $?
expect_in_log "D3 names the stale-entry guard" "$ROOT/d3.log" \
  "test_every_declared_suppression_still_exists"

T="$(make_tree d4)"
edit "$T" 'sub("tests/test_suppressions.py",
    "test_a_non_http_confluence_url_is_refused_before_any_request\",",
    "test_that_was_never_written\",")'
run_pytest "$T" "$ROOT/d4.log" tests/test_suppressions.py
expect_exit "D4 a justification backed by a test that does not exist: guard fires" 1 $?
expect_in_log "D4 names the missing test" "$ROOT/d4.log" "test_that_was_never_written"

# ---------------------------------------------------------------------------
# E. The pipeline file itself.
# ---------------------------------------------------------------------------

printf -- '\n-- E. pipeline contract --\n'

T="$(make_tree e0)"
run_pytest "$T" "$ROOT/e0.log" tests/test_pipeline_contract.py
expect_exit "E0 unmutated tree: the pipeline contract holds" 0 $?

T="$(make_tree e1)"
mv "$T/scripts/check-version.sh" "$T/scripts/check-version.sh.gone"
run_pytest "$T" "$ROOT/e1.log" tests/test_pipeline_contract.py
expect_exit "E1 a step runs a file nobody committed: guard fires" 1 $?
expect_in_log "E1 names the missing path" "$ROOT/e1.log" "scripts/check-version.sh"

T="$(make_tree e2)"
edit "$T" 'sub(".github/workflows/ring1.yml", "semgrep==1.175.0", "semgrep")'
run_pytest "$T" "$ROOT/e2.log" tests/test_pipeline_contract.py
expect_exit "E2 a floating scanner version: guard fires" 1 $?
expect_in_log "E2 names the unpinned scanner" "$ROOT/e2.log" "semgrep is not pinned"

T="$(make_tree e3)"
edit "$T" 'sub(".github/workflows/ring1.yml",
    "    name: \"Diff coverage (threshold from pyproject.toml)\"",
    "    name: \"Diff coverage renamed\"")'
run_pytest "$T" "$ROOT/e3.log" tests/test_pipeline_contract.py
expect_exit "E3 a gate job that is no longer declared: guard fires" 1 $?
expect_in_log "E3 names the missing gate" "$ROOT/e3.log" "Diff coverage"

T="$(make_tree e4)"
edit "$T" 'sub(".github/workflows/ring1.yml", "\njobs:\n", "\njobs:\n  ][\n")'
run_pytest "$T" "$ROOT/e4.log" tests/test_pipeline_contract.py
expect_exit "E4 a workflow file GitHub could not parse: guard fires" 1 $?

T="$(make_tree e6)"
edit "$T" 'sub(".github/workflows/ring1.yml",
    "\njobs:\n", "\njobs:\n  stale-report:\n    name: \"Notify owners\"\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n")'
run_pytest "$T" "$ROOT/e6.log" tests/test_pipeline_contract.py
expect_exit "E6 the weekly report wired in as a merge gate: guard fires" 1 $?
expect_in_log "E6 says a report is not a gate" "$ROOT/e6.log" "test_the_report_is_never_part_of_the_gate"

T="$(make_tree e6b)"
edit "$T" 'sub(".github/workflows/ring1.yml", "  workflow_dispatch:\n\npermissions:", "  workflow_dispatch:\n  schedule:\n    - cron: \"0 3 * * *\"\n\npermissions:")'
run_pytest "$T" "$ROOT/e6b.log" tests/test_pipeline_contract.py
expect_exit "E6b a schedule trigger added to the gate: guard fires" 1 $?
expect_in_log "E6b names the trigger contract" "$ROOT/e6b.log" "test_each_workflow_declares_exactly_its_triggers"

T="$(make_tree e6c)"
edit "$T" 'sub(".github/workflows/ring1.yml", "    if: github.event_name != '"'"'pull_request'"'"'\n", "")'
run_pytest "$T" "$ROOT/e6c.log" tests/test_pipeline_contract.py
expect_exit "E6c the image job runs on pull requests: guard fires" 1 $?
expect_in_log "E6c names the required-checks contract" "$ROOT/e6c.log" "test_a_pull_request_runs_exactly_the_required_checks"

T="$(make_tree e6d)"
edit "$T" 'sub(".github/workflows/release-check.yml",
    "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0", "actions/setup-python@v5.6.0")'
run_pytest "$T" "$ROOT/e6d.log" tests/test_pipeline_contract.py
expect_exit "E6d an action pinned by tag instead of SHA: guard fires" 1 $?
expect_in_log "E6d names the unpinned action" "$ROOT/e6d.log" "not pinned to a commit SHA"

# ---------------------------------------------------------------------------
# F. Layered architecture: import-linter (ADR-022). Runs the real CI command,
#    against a copy — never the tree this repository actually ships.
# ---------------------------------------------------------------------------

printf -- '\n-- F. layered architecture (import-linter) --\n'

T="$(make_tree f0)"
run_lint_imports "$T" "$ROOT/f0.log"
expect_exit "F0 unmutated tree: every import contract holds" 0 $?
expect_in_log "F0 reports the contract kept" "$ROOT/f0.log" "KEPT"

# The lowest layer reaching for the top one — exactly the direction ADR-022
# forbids, and the one a careless "just import what you need" fix would add.
T="$(make_tree f1)"
edit "$T" 'sub("src/knowledge/config.py",
    "from __future__ import annotations\n",
    "from __future__ import annotations\n\nfrom knowledge import server as _canary_forbidden_upward_import\n")'
run_lint_imports "$T" "$ROOT/f1.log"
expect_exit "F1 config reaches up to server: guard fires" 1 $?
expect_in_log "F1 names the forbidden edge" "$ROOT/f1.log" \
  "knowledge.config is not allowed to import knowledge.server"

# A sibling importing a sibling: auth and webhook share one "|" line under
# server, so neither may import the other, even though both already
# legitimately import config.
T="$(make_tree f2)"
edit "$T" 'sub("src/knowledge/auth.py",
    "from knowledge import config\n",
    "from knowledge import config\nfrom knowledge import webhook as _canary_sibling_violation\n")'
run_lint_imports "$T" "$ROOT/f2.log"
expect_exit "F2 a sibling imports a sibling: guard fires" 1 $?
expect_in_log "F2 names the forbidden edge" "$ROOT/f2.log" \
  "knowledge.auth is not allowed to import knowledge.webhook"

# The contract file itself going missing must not read as "nothing to check".
T="$(make_tree f3)"
rm -f "$T/.importlinter"
run_lint_imports "$T" "$ROOT/f3.log"
expect_exit "F3 the contract file is gone: the step fails, not a silent pass" 1 $?

# The core reaching for an extension: indexer is core and gitsync is ext, so
# both the layer order and the core-isolated contract forbid the edge.
T="$(make_tree f4)"
edit "$T" 'sub("src/knowledge/indexer.py",
    "from knowledge import errors\n",
    "from knowledge import errors\nfrom knowledge import gitsync as _canary_core_reaches_ext\n")'
run_lint_imports "$T" "$ROOT/f4.log"
expect_exit "F4 core imports an extension: guard fires" 1 $?
expect_in_log "F4 names the forbidden edge" "$ROOT/f4.log" \
  "knowledge.indexer is not allowed to import knowledge.gitsync"

# The core reaching for a transport library: the layer order says nothing
# about third-party packages, so only core-no-transport catches this edge.
T="$(make_tree f5)"
edit "$T" 'sub("src/knowledge/indexer.py",
    "from knowledge import errors\n",
    "from knowledge import errors\nimport starlette as _canary_core_reaches_transport\n")'
run_lint_imports "$T" "$ROOT/f5.log"
expect_exit "F5 core imports a transport library: guard fires" 1 $?
expect_in_log "F5 names the forbidden edge" "$ROOT/f5.log" \
  "knowledge.indexer is not allowed to import starlette"

# ---------------------------------------------------------------------------
# G. Semgrep, for real. Opt-in: pulls the pinned image.
# ---------------------------------------------------------------------------

printf -- '\n-- G. semgrep (opt-in) --\n'

run_semgrep() {
  local tree="$1" log="$2"
  docker run --rm -v "$tree":/src -w /src --platform linux/amd64 "$SEMGREP_IMAGE" \
    semgrep --config p/python --config p/security-audit --error --metrics=off src/ scripts/ \
    > "$log" 2>&1
  return $?
}

if [ "$WITH_SEMGREP" -eq 1 ]; then
  if command -v docker > /dev/null 2>&1; then
    T="$(make_tree g1)"
    run_semgrep "$T" "$ROOT/g1.log"
    expect_exit "G1 tree as shipped: the SAST step is green" 0 $?
    expect_in_log "G1 green means zero findings" "$ROOT/g1.log" "Findings: 0"

    T="$(make_tree g2)"
    edit "$T" 'sub("src/knowledge/confluence_sync.py",
        "            " + MARK + ": python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected\n",
        "")'
    run_semgrep "$T" "$ROOT/g2.log"
    expect_exit "G2 suppression removed: the same rule blocks the merge again" 1 $?
    expect_in_log "G2 the zero was the comment, not the code" "$ROOT/g2.log" "Findings: 1"
    expect_in_log "G2 names the rule still matching" "$ROOT/g2.log" "dynamic-urllib-use-detected"
  else
    fail "G semgrep cases requested but docker is not available"
  fi
else
  printf '[SKIP] G semgrep cases (pass --with-semgrep to run them)\n'
fi

printf '\n== summary ==\nPASS: %d  FAIL: %d\n' "$PASS_N" "$FAIL_N"
[ "$FAIL_N" -eq 0 ] || exit 1
exit 0
