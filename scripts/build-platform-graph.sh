#!/usr/bin/env bash
# build-platform-graph.sh — reproducible build of the platform code graph.
#
# Reads the per-repository graphs produced by `graphify update` and fuses them with
# `graphify merge-graphs` into a single cross-repo artifact plus a provenance sidecar.
#
# Why the sidecar exists: `graphify merge-graphs` is an `nx.compose` of graphs passed
# through `prefix_graph_for_global`. Its output keeps only five top-level keys
# (directed, multigraph, graph, nodes, links) and DROPS `built_at_commit`. ADR-026
# enforces `CHECK (graph_summary IS NULL OR graph_commit IS NOT NULL)` on
# `project_cards`, so a platform card is impossible unless the commit each source
# graph was built from is preserved out of band. This script preserves it.
#
# Read-only over service repositories: nothing is ever written under a repository used
# as a source. Building inside --out is refused for the same reason a copied vendor
# tree used to be staged outside every repository: graphify walks `.graphifyignore` and
# `.gitignore` up to the enclosing VCS root, so a build directory placed under a
# repository whose ignore file swallows it would produce an empty graph.
#
# Default source set: `graph.targets` in config.json (ADR-039), read through the
# repository registry (`knowledge.graph_targets.graph_targets_detail()`, `uv run` from this
# checkout). Each declared (repo, path) resolves to a directory on this machine or it
# does not; when it does, its graph is fused only if `graphify-out/graph.json` is
# already there. Nothing is built here -- that is `bootstrap-graph`'s job, one repo at
# a time. A target this script cannot fuse is never silently absent: it is named in
# `merged-graph.provenance.json` under `skipped_targets`, with the reason it did not
# make it in (unresolved on this machine, no graph built yet, or stale against its
# repository's HEAD). This is a deliberate change from an earlier version of this
# script, which auto-staged a vendored package tree into a scratch
# build directory because a copy of it has no `.git` of its own. The vendor CHECKOUT
# does have its own `.git` (verified: `git -C <vendor checkout> rev-parse HEAD`
# resolves), so per the bootstrap-graph skill it needs no staging at all -- it is read
# in place like any other `graph.targets` entry now, and the staging code is retired.
#
# When a target's canonical checkout is busy (checked out to a different branch with
# a developer's uncommitted work on it, e.g. `apps/portal` mid-feature) this script
# still refuses to touch it -- but the target does not have to stay skipped forever.
# The registry resolves each repo id through `CD_REPO_<ID>` before falling back to its
# configured root (`src/knowledge/repos.py`, `repo_env_var`), so a *second*, disposable
# checkout of the same branch this script wants to read from settles it without ever
# touching the busy one:
#
#   git worktree add /tmp/portal-graph chore/ai-architecture   # from the canonical checkout
#   cd /tmp/portal-graph && rm -rf graphify-out && graphify update .
#   CD_REPO_PORTAL=/tmp/portal-graph bash scripts/build-platform-graph.sh
#   git -C /tmp/portal-graph worktree remove --force /tmp/portal-graph  # then, from the
#                                                                        # canonical checkout
#
# The worktree is disposable on purpose: this recipe, not the directory, is what makes a
# 14/14-fused run reproducible. Re-run the four lines above any time the target's
# canonical checkout is still busy and its graph has gone stale again -- the busy
# checkout itself is never touched, never has its branch switched, and never gains a
# commit from this.
#
# Requirements: bash 3.2 (the /bin/bash macOS ships), graphify, python3, uv, git, rg,
# and scripts/lib/scan-credential-shapes.py next to this file.
#
# Every gate distinguishes "I looked and found nothing" from "I could not look".
# The second is never a pass: an unreadable repository, a scanner that will not
# run or a git command that fails stops the run instead of publishing on a blank.
#
# Exit codes:
#   0  merged graph and sidecar written (possibly with declared targets skipped --
#      that is recorded, not silent; see skipped_targets in the sidecar)
#   2  usage error, missing input, a declared repository that git cannot read,
#      two sources resolving to the same merge tag, or fewer than two sources
#      survive discovery/staleness filtering
#   3  a source graph has no `built_at_commit` (ADR-026 refuses a graph fact without one)
#   4  an EXPLICIT `--source` graph is stale against its repository HEAD (override
#      with --allow-stale); an auto-discovered graph.targets source is instead
#      skipped and named in skipped_targets unless --allow-stale is passed, in which
#      case it is included with fresh:false, same as an explicit stale source
#   5  the credential scan did not come back clean -- it matched a shape, it could not
#      run, it failed the self-probe that proves it can still tell a credential from an
#      identifier, or it reported inspecting fewer lines than the artifact holds;
#      nothing was published in any of those cases
#   6  retired. Used to gate a dirty vendor tree being staged; no longer produced now
#      that vendor staging is gone (see above). Kept out of reuse so old references to
#      "exit 6" in the plan are not silently reinterpreted as something else.
#   7  graph construction or `graphify merge-graphs` failed
#   8  two sources come from two different checkouts of the SAME repository. The
#      platform graph reads a repository through one checkout only: a repository
#      may sit on disk several times, one deployment slot each, and fusing
#      surfaces taken from two of those slots describes a tree that exists in
#      neither of them. Fires on the pair of
#      checkouts, not on the pair of commits -- two slots can agree on HEAD today
#      and diverge on the next commit, so the commit is the symptom and the split
#      declaration is the defect

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KNOWLEDGE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DEFAULT_OUT="$KNOWLEDGE_ROOT/platform-graph"

OUT_DIR="${PLATFORM_GRAPH_OUT:-$DEFAULT_OUT}"
ALLOW_STALE=0

SRC_GRAPH=()
SRC_REPO=()
# Parallel to SRC_GRAPH/SRC_REPO. SRC_AUTO=1 marks a source discovered from
# graph.targets (eligible to be skipped-not-died-on when stale); SRC_META carries
# its declared "repo:path" for the provenance sidecar. Both are empty/0 for an
# explicit --source, which never carries that identity and is never soft-skipped.
SRC_AUTO=()
SRC_META=()

usage() {
  printf '%s\n' \
    'build-platform-graph.sh — build the platform code graph from per-repo graphs.' \
    '' \
    'Usage:' \
    '  build-platform-graph.sh [options]' \
    '' \
    'Options:' \
    '  --out DIR            Output directory for the artifacts.' \
    '                       Default: <knowledge-mcp>/platform-graph' \
    '  --source GRAPH[::REPO]' \
    '                       Use an explicit source instead of the default set. Repeatable.' \
    '                       GRAPH is a graph.json; REPO is the git checkout it was built' \
    '                       from (default: the graph grandparent directory). Passing any' \
    '                       --source disables the default set: graph.targets discovery' \
    '                       does not run at all.' \
    '  --allow-stale        Include a stale source instead of refusing it. For an' \
    '                       explicit --source this downgrades the gate from a hard' \
    '                       failure to a warning; for an auto-discovered graph.targets' \
    '                       source it is the difference between silently SKIPPING it' \
    '                       (the default) and including it with fresh:false.' \
    '  -h, --help           Print this help.' \
    '' \
    'Default source set: every entry declared under graph.targets in config.json' \
    '(ADR-039), resolved through the repository registry. A declared entry is fused' \
    'only when it resolves to a directory on this machine AND that directory already' \
    'has graphify-out/graph.json (built by bootstrap-graph, or by hand). Everything' \
    'else -- unresolved on this machine, no graph built yet, or stale against its' \
    'repository HEAD without --allow-stale -- is skipped and named, never silently' \
    'dropped: see skipped_targets in merged-graph.provenance.json.' \
    '' \
    'Artifacts written to --out:' \
    '  merged-graph.json              the fused cross-repo graph' \
    '  merged-graph.provenance.json   per-source commits, hashes, counts (ADR-026) and' \
    '                                 every declared target this run did not fuse' \
    '  merged-graph.report.md         operator-facing summary'
}

die() { printf 'error: %s\n' "$1" >&2; exit "${2:-2}"; }
note() { printf '%s\n' "$1"; }
# Echo a file to stderr without cat.
emit_stderr() { rg -N '^' "$1" >&2 || true; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --out) [ "$#" -ge 2 ] || die "--out needs a value"; OUT_DIR="$2"; shift 2 ;;
    --source)
      [ "$#" -ge 2 ] || die "--source needs a value"
      case "$2" in
        *"::"*)
          SRC_GRAPH[${#SRC_GRAPH[@]}]="${2%%::*}"
          SRC_REPO[${#SRC_REPO[@]}]="${2#*::}"
          ;;
        *)
          SRC_GRAPH[${#SRC_GRAPH[@]}]="$2"
          SRC_REPO[${#SRC_REPO[@]}]=""
          ;;
      esac
      # An explicit source carries no graph.targets identity and is never
      # soft-skipped for staleness -- see the loop below.
      SRC_AUTO[${#SRC_AUTO[@]}]=0
      SRC_META[${#SRC_META[@]}]=""
      shift 2 ;;
    --allow-stale) ALLOW_STALE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument '$1'" ;;
  esac
done

for tool in graphify python3 uv git rg; do
  command -v "$tool" > /dev/null 2>&1 || die "required tool not found: $tool"
done

CRED_SCANNER="$SCRIPT_DIR/lib/scan-credential-shapes.py"
[ -f "$CRED_SCANNER" ] || die "credential scanner not found: $CRED_SCANNER"

if command -v sha256sum > /dev/null 2>&1; then
  hash_of() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum > /dev/null 2>&1; then
  hash_of() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
  die "neither sha256sum nor shasum is available"
fi

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

MERGED="$OUT_DIR/merged-graph.json"
MERGED_TMP="$OUT_DIR/.merged-graph.tmp.json"
PROVENANCE="$OUT_DIR/merged-graph.provenance.json"
REPORT="$OUT_DIR/merged-graph.report.md"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/platform-graph.XXXXXX")"
cleanup() {
  rm -rf "$WORK"
  rm -f "$MERGED_TMP"
}
trap cleanup EXIT

# --------------------------------------------------------------------------- #
# Prove the credential scanner still works before believing anything it says.
#
# `[ -f "$CRED_SCANNER" ]` only establishes that a file is there. A truncated, empty
# or stubbed scanner exits 0 on every input, and an exit 0 taken on faith turns the
# publish gate into a decoration that reports "clean" over a graph nobody read. So the
# scanner is run against two fixtures whose verdict is known here, and both exit codes
# are required by EXACT equality: a scanner that cannot find a planted credential is
# broken, and so is one that flags a plain environment-variable name.
#
# The planted value is assembled at runtime from the words CANARY and FAKE, in the
# shape of an `authpt` query parameter, so no real secret is ever written to disk.
# --------------------------------------------------------------------------- #
probe_credential_scanner() {
  local probe_dir="$WORK/scanner-probe"
  local positive="$probe_dir/positive.txt"
  local negative="$probe_dir/negative.txt"
  local planted rc

  mkdir -p "$probe_dir"
  planted="$(printf '%s' 'C4n4RyF4k3' 'Pr0b3V4lu3' 'AbCd3fGh1j' 'KlMn0pQrSt')"
  printf '  "label": "authpt=%s",\n' "$planted" > "$positive"
  printf '  "label": "JIRA_API_TOKEN",\n' > "$negative"

  rc=0
  python3 "$CRED_SCANNER" "$positive" > "$probe_dir/positive.log" 2>&1 || rc=$?
  if [ "$rc" -ne 1 ]; then
    die "the credential scanner failed its self-probe: a planted credential shape came back as exit $rc instead of 1, so the scan could not run as a gate and an unproven scanner is not a clean scan; nothing was published" 5
  fi

  rc=0
  python3 "$CRED_SCANNER" "$negative" > "$probe_dir/negative.log" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    die "the credential scanner failed its self-probe: a plain environment-variable name came back as exit $rc instead of 0, so the scan could not run as a gate and every later verdict would be noise; nothing was published" 5
  fi

  # A clean verdict has to arrive with its inspection count attached. Without the
  # marker there is no evidence the file was opened at all.
  if ! rg -q '^credential-scan: clean rules=[0-9]+ heuristics=[0-9]+ lines=[0-9]+ chars=[0-9]+$' "$probe_dir/negative.log"; then
    die "the credential scanner reported a clean file without saying how much it inspected, so its exit 0 could not run as evidence; nothing was published" 5
  fi
}
probe_credential_scanner

# --------------------------------------------------------------------------- #
# Source set: graph.targets in config.json (ADR-039), read through the registry.
#
# One entry may resolve to a directory that this machine cannot reach (no root
# configured, or resolved but not on disk), or to a directory with no graph built
# yet, or to a graph that is stale against its own repository's HEAD. None of
# those stop the run: they are named in $SKIPPED_JSONL (one JSON object per
# line), which the final report folds into skipped_targets. Nothing here builds
# a graph -- a missing graph is bootstrap-graph's job, not this script's.
# --------------------------------------------------------------------------- #
SKIPPED_JSONL="$WORK/skipped.jsonl"
: > "$SKIPPED_JSONL"
# Sibling checkouts: registry entries that are another copy of a repository this
# run reads, and that the platform graph therefore does NOT read. They are not
# inputs and never gate anything; they are surveyed so the artifact can say out
# loud which working trees it leaves outside itself. See the survey below.
SIBLINGS_TSV="$WORK/siblings.tsv"
: > "$SIBLINGS_TSV"
DISCOVERY_USED=0

if [ "${#SRC_GRAPH[@]}" -eq 0 ]; then
  DISCOVERY_USED=1
  note "No --source given: discovering the default set from graph.targets (config.json)..."
  CANDIDATES_TSV="$WORK/candidates.tsv"
  : > "$CANDIDATES_TSV"

  discover_rc=0
  ( cd "$KNOWLEDGE_ROOT" && uv run python3 - "$CANDIDATES_TSV" "$SKIPPED_JSONL" "$SIBLINGS_TSV" <<'PY'
import json
import os
import sys

from knowledge import graph_targets

candidates_path, skipped_path, siblings_path = sys.argv[1:4]

targets = graph_targets.graph_targets_detail()

# Every other checkout of a repository some target reads. The registry is the only
# place that knows they exist: git can name the checkout it is standing in, never
# its siblings. Written before the targets are filtered, because a sibling is a
# fact about the machine and does not depend on whether the target had a graph.
checkouts = graph_targets.repo_checkouts()
remote_of = {c["id"]: c["remote"] for c in checkouts}
declared_ids = {t["repo"] for t in targets if t["repo"]}
declared_remotes = {remote_of[i] for i in declared_ids if i in remote_of}
with open(siblings_path, "w", encoding="utf-8") as sib_fh:
    for checkout in checkouts:
        if checkout["remote"] not in declared_remotes or checkout["id"] in declared_ids:
            continue
        sib_fh.write("\t".join([
            checkout["remote"],
            checkout["id"],
            checkout["resolved_dir"] or "",
            checkout["reason"],
        ]) + "\n")

with open(candidates_path, "w", encoding="utf-8") as cand_fh, \
        open(skipped_path, "a", encoding="utf-8") as skip_fh:
    for item in targets:
        repo_id = item["repo"] or ""
        subpath = item["path"]
        label = "{0}:{1}".format(repo_id, subpath) if repo_id else subpath
        resolved_dir = item["resolved_dir"]
        if resolved_dir is None:
            skip_fh.write(json.dumps({
                "target": label,
                "reason": "unresolved on this machine: {0}".format(item["reason"]),
                "stage": "unresolved",
            }) + "\n")
            continue
        graph_path = os.path.join(resolved_dir, "graphify-out", "graph.json")
        if not os.path.isfile(graph_path):
            skip_fh.write(json.dumps({
                "target": label,
                "reason": "no graph built yet: {0} does not exist".format(graph_path),
                "stage": "missing_graph",
            }) + "\n")
            continue
        cand_fh.write("\t".join([resolved_dir, graph_path, repo_id, subpath]) + "\n")
PY
  ) > "$WORK/discover.log" 2>&1 || discover_rc=$?
  if [ "$discover_rc" -ne 0 ]; then
    emit_stderr "$WORK/discover.log"
    die "could not read graph.targets through 'uv run python3' + knowledge.config (exit $discover_rc); nothing was discovered"
  fi

  n_candidates=0
  while IFS=$'\t' read -r cand_dir cand_graph cand_repo cand_path; do
    [ -n "$cand_dir" ] || continue
    SRC_GRAPH[${#SRC_GRAPH[@]}]="$cand_graph"
    SRC_REPO[${#SRC_REPO[@]}]="$cand_dir"
    SRC_AUTO[${#SRC_AUTO[@]}]=1
    if [ -n "$cand_repo" ]; then
      SRC_META[${#SRC_META[@]}]="$cand_repo:$cand_path"
    else
      SRC_META[${#SRC_META[@]}]="$cand_path"
    fi
    n_candidates=$((n_candidates + 1))
  done < "$CANDIDATES_TSV"

  n_skipped_discovery=0
  if [ -s "$SKIPPED_JSONL" ]; then
    n_skipped_discovery="$(rg -c '' "$SKIPPED_JSONL")" || n_skipped_discovery=0
  fi
  note "graph.targets: $n_candidates target(s) with a graph to consider, $n_skipped_discovery skipped at discovery."
  while IFS= read -r skip_line; do
    [ -n "$skip_line" ] || continue
    reason="$(printf '%s\n' "$skip_line" | python3 -c 'import json,sys; print(json.load(sys.stdin)["reason"])')"
    note "  skipped: $reason"
  done < "$SKIPPED_JSONL"
fi

N_SOURCES="${#SRC_GRAPH[@]}"
if [ "$N_SOURCES" -lt 2 ]; then
  die "at least two source graphs are required; got $N_SOURCES"
fi

# --------------------------------------------------------------------------- #
# Gates over the sources
# --------------------------------------------------------------------------- #
SOURCES_JSON="$WORK/sources.json"
: > "$WORK/tags.txt"
# One line per fused source: <repository identity>\t<checkout root>\t<tag>. The
# one-checkout-per-repository gate reads it after the loop, once the set of
# sources that actually survived is known.
: > "$WORK/identities.tsv"
printf '[\n' > "$SOURCES_JSON"
# Counts entries actually WRITTEN to SOURCES_JSON, which is not the same as the
# loop index `i` once a source can be skipped mid-loop (see the staleness branch
# below): the comma before an entry has to reflect what was written, not how many
# iterations ran.
WRITTEN=0

i=0
while [ "$i" -lt "$N_SOURCES" ]; do
  graph="${SRC_GRAPH[$i]}"
  repo="${SRC_REPO[$i]}"
  auto="${SRC_AUTO[$i]:-0}"
  meta="${SRC_META[$i]:-}"
  [ -f "$graph" ] || die "source graph not found: $graph"

  graph_abs="$(cd "$(dirname "$graph")" && pwd)/$(basename "$graph")"
  SRC_GRAPH[i]="$graph_abs"

  # `graphify merge-graphs` derives the namespace prefix from the grandparent
  # directory of each graph.json. Two sources sharing a tag would be fused into a
  # single namespace by nx.compose instead of kept apart.
  tag="$(basename "$(dirname "$(dirname "$graph_abs")")")"
  # rg exits 0 on a match, 1 on no match and 2 on an error. Folding 2 into "no
  # duplicate" would turn a broken read into a silent pass.
  dup_rc=0
  rg -qxF "$tag" "$WORK/tags.txt" || dup_rc=$?
  case "$dup_rc" in
    0) die "two sources resolve to the same merge tag '$tag'; nx.compose would fuse them" ;;
    1) : ;;
    *) die "could not check the merge tag of '$graph_abs' (rg exit $dup_rc)" ;;
  esac
  printf '%s\n' "$tag" >> "$WORK/tags.txt"

  if [ -z "$repo" ]; then repo="$(dirname "$(dirname "$graph_abs")")"; fi

  commit="$(python3 -c 'import json,sys
with open(sys.argv[1], encoding="utf-8") as fh:
    print(json.load(fh).get("built_at_commit") or "")' "$graph_abs")"
  if [ -z "$commit" ]; then
    die "source graph has no built_at_commit: $graph_abs (ADR-026 forbids a graph fact without a commit)" 3
  fi

  head=""
  fresh="null"
  dirty_files=-1
  repo_root=""
  repo_branch=""
  repo_identity=""
  # A declared target is often a SUBDIRECTORY of a checkout rather than its root:
  # graph.targets may carry `<repo>:lib`, `<repo>:apps` and their kin, and a
  # subdirectory holds no `.git` of its own. Testing only `$repo/.git` records
  # `fresh:null` on every such surface -- typically the largest sources in the
  # artifact, going in with their staleness never checked. So the probe walks up
  # until it finds a `.git` entry or runs out of parents.
  #
  # The walk tests for the ENTRY, not for a repository git agrees to open, and that
  # distinction is the gate: an empty or corrupt `.git` is "present but unreadable",
  # which has to fail loudly below, while asking git to resolve the top level would
  # quietly demote it to "no repository" and publish on an unchecked source.
  vcs_probe="$repo"
  found_git=0
  while : ; do
    if [ -e "$vcs_probe/.git" ]; then found_git=1; break; fi
    vcs_parent="$(dirname "$vcs_probe")"
    [ "$vcs_parent" = "$vcs_probe" ] && break
    vcs_probe="$vcs_parent"
  done
  if [ "$found_git" -eq 1 ]; then
    # A repository that is present but unreadable is not a fresh source and not a
    # stale one: it is a source whose staleness could not be established. Failing
    # here is the difference between a gate and a decoration.
    head="$(git -C "$repo" rev-parse HEAD 2> "$WORK/git-err.txt")" || {
      emit_stderr "$WORK/git-err.txt"
      die "cannot read HEAD of the repository declared for '$tag': $repo"
    }
    [ -n "$head" ] || die "git returned an empty HEAD for the repository declared for '$tag': $repo"

    # Which checkout this surface belongs to, and which repository that checkout is
    # a copy of. The two are not the same question: one repository may have several
    # checkouts on a machine, so the root separates them and only the remote says
    # they are the same repository. The gate after the loop needs both.
    repo_root="$(git -C "$repo" rev-parse --show-toplevel 2> "$WORK/git-err.txt")" || {
      emit_stderr "$WORK/git-err.txt"
      die "cannot read the checkout root of the repository declared for '$tag': $repo"
    }
    [ -n "$repo_root" ] || die "git returned an empty checkout root for '$tag': $repo"

    # The branch, recorded because the commit alone hides the divergence that
    # matters: two checkouts of one repository can sit on different branches and
    # still agree on HEAD until the next commit lands on one of them. A detached
    # HEAD has no branch and `--abbrev-ref` answers "HEAD"; that is kept verbatim.
    repo_branch="$(git -C "$repo" rev-parse --abbrev-ref HEAD 2> /dev/null)" || repo_branch=""

    # No origin means "identity unknown", never "identity matches". An unknown
    # identity falls back to the checkout root, which can only ever collide with
    # itself, so the gate below never fires on a guess.
    repo_identity="$(git -C "$repo" config --get remote.origin.url 2> /dev/null)" || repo_identity=""
    repo_identity="${repo_identity%/}"
    repo_identity="${repo_identity%.git}"
    if [ -z "$repo_identity" ]; then
      repo_identity="checkout:$repo_root"
      note "warning: '$tag' declares no origin remote; its repository identity falls back to its own checkout root, so it cannot be compared with any other source"
    fi

    # The freshness gate compares commits, so a graph can match HEAD and still miss
    # every uncommitted change in the tree. That is a warning, not a failure: the
    # merge is still reproducible from the commits the sidecar records.
    # Scoped with `-- .` so a subdirectory target reports the dirt of the surface
    # that was actually read, not of the whole enclosing checkout.
    # `git status | wc -l` would report the exit status of wc, so git runs alone.
    git -C "$repo" status --porcelain -- . > "$WORK/status.txt" 2> "$WORK/git-err.txt" || {
      emit_stderr "$WORK/git-err.txt"
      die "cannot read the working tree state of the repository declared for '$tag': $repo"
    }
    dirty_rc=0
    dirty_files="$(rg -c '' "$WORK/status.txt")" || dirty_rc=$?
    case "$dirty_rc" in
      0) : ;;
      1) dirty_files=0 ;;
      *) die "could not count the uncommitted changes of '$tag' (rg exit $dirty_rc)" ;;
    esac
    if [ "$dirty_files" -gt 0 ]; then
      note "warning: '$tag' has $dirty_files uncommitted change(s); the graph reflects ${commit:0:12}, not the working tree"
    fi

    if [ "$head" = "$commit" ]; then
      fresh="true"
    else
      fresh="false"
      if [ "$ALLOW_STALE" -eq 0 ]; then
        if [ "$auto" -eq 1 ]; then
          # An auto-discovered graph.targets source: staleness here is a fact
          # about someone else's repository, not a reason to refuse the whole
          # platform graph. Skip it, name it, keep going -- an explicit
          # --source still dies below, because there staleness is something
          # the caller asked for by hand and should notice immediately.
          note "warning: skipping stale auto-discovered source '$tag' (${meta:-no declared target}): graph ${commit:0:12} != HEAD ${head:0:12} (pass --allow-stale to include it anyway)"
          python3 -c 'import json,sys
print(json.dumps({"target": sys.argv[1] or sys.argv[2], "reason":
    "stale: graph built from {0} but repo HEAD is {1} (rerun graphify update there, or pass --allow-stale)".format(sys.argv[3][:12], sys.argv[4][:12]),
    "stage": "stale_skip"}))' "$meta" "$tag" "$commit" "$head" >> "$SKIPPED_JSONL"
          unset "SRC_GRAPH[$i]"
          i=$((i + 1))
          continue
        fi
        die "stale source graph: $graph_abs was built from ${commit:0:12} but $repo is at ${head:0:12} (run 'graphify update' there, or pass --allow-stale)" 4
      fi
      note "warning: stale source '$tag' (graph ${commit:0:12} != HEAD ${head:0:12})"
    fi
  else
    # No repository at all is a declared absence, not a failed read: it is recorded
    # as fresh:null in the sidecar so nobody mistakes it for a checked source.
    note "warning: no git checkout contains '$repo' for source '$tag'; its staleness was NOT checked (sidecar records fresh:null)"
  fi

  counts="$(python3 -c 'import json,sys
with open(sys.argv[1], encoding="utf-8") as fh:
    d = json.load(fh)
print(len(d.get("nodes") or []), len(d.get("links") or d.get("edges") or []))' "$graph_abs")" \
    || die "could not read the node and link counts of $graph_abs"
  nodes="${counts%% *}"
  links="${counts##* }"
  sha="$(hash_of "$graph_abs")"

  # Recorded here, past the staleness branch, so a source skipped mid-loop never
  # reaches the gate: what is not fused cannot conflict with what is.
  if [ -n "$repo_identity" ]; then
    printf '%s\t%s\t%s\n' "$repo_identity" "$repo_root" "$tag" >> "$WORK/identities.tsv"
  fi

  if [ "$WRITTEN" -gt 0 ]; then printf ',\n' >> "$SOURCES_JSON"; fi
  python3 - "$SOURCES_JSON" "$tag" "$graph_abs" "$repo" "$commit" "$head" "$fresh" "$nodes" "$links" "$sha" "$dirty_files" "$meta" "$repo_root" "$repo_branch" "$repo_identity" <<'PY'
import json, os, sys
out, tag, graph, repo, commit, head, fresh, nodes, links, sha, dirty, meta = sys.argv[1:13]
repo_root, repo_branch, repo_identity = sys.argv[13:16]
entry = {
    "tag": tag,
    "graph_path": graph,
    "graph_sha256": sha,
    "graph_bytes": os.path.getsize(graph),
    "repo_path": repo,
    # The checkout `repo_path` lives in, and the repository that checkout copies.
    # A subpath target ("<repo>:lib") has a repo_path far below its root, and the
    # identity is what tells two slots of one repository apart from two unrelated
    # repositories.
    "repo_root": repo_root or None,
    "repo_identity": repo_identity or None,
    # Recorded next to the commit because the commit alone cannot show a branch
    # divergence that has not produced a commit yet.
    "repo_branch": repo_branch or None,
    "built_at_commit": commit,
    "repo_head_at_merge": head or None,
    "fresh": {"true": True, "false": False}.get(fresh),
    "uncommitted_files": None if int(dirty) < 0 else int(dirty),
    "nodes": int(nodes),
    "links": int(links),
    # The declared graph.targets "repo:path" this source came from, when it was
    # auto-discovered rather than named by an explicit --source.
    "declared_target": meta or None,
}
with open(out, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry, ensure_ascii=False, indent=2))
PY
  note "source $tag: $nodes nodes, $links links, commit ${commit:0:12}"
  WRITTEN=$((WRITTEN + 1))
  i=$((i + 1))
done
printf '\n]\n' >> "$SOURCES_JSON"

# --------------------------------------------------------------------------- #
# One checkout per repository
#
# The platform graph lets a repository in through exactly one checkout. Reading
# one surface from one slot of a repository and another surface from a second
# slot fuses two working trees into one artifact that describes a state existing
# in neither, and nothing else in this script would notice: the freshness gate
# compares each source against ITS OWN repository HEAD, so two sources of one
# repository sitting on two commits both report fresh.
#
# The gate is on the pair of CHECKOUTS, not on the pair of commits. Two slots can
# agree on HEAD and diverge the moment one of them takes a commit. Passing
# because the commits happen to match would
# leave the split declaration in place until the artifact was already wrong, which
# is the failure this gate exists to prevent.
# --------------------------------------------------------------------------- #
identity_rc=0
python3 - "$WORK/identities.tsv" <<'PY' || identity_rc=$?
import collections, sys

roots_of = collections.OrderedDict()
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        line = line.rstrip("\n")
        if not line:
            continue
        identity, root, tag = line.split("\t", 2)
        roots_of.setdefault(identity, collections.OrderedDict()).setdefault(root, []).append(tag)

conflicts = [(i, r) for i, r in roots_of.items() if len(r) > 1]
if not conflicts:
    sys.exit(0)

for identity, roots in conflicts:
    print(
        "repository '{0}' entered through {1} checkouts:".format(identity, len(roots)),
        file=sys.stderr,
    )
    for root, tags in roots.items():
        print("    {0}  <- {1}".format(root, ", ".join(tags)), file=sys.stderr)
sys.exit(1)
PY
case "$identity_rc" in
  0) : ;;
  1) die "the platform graph reads one repository through more than one checkout; declare every surface of it from a single checkout in graph.targets (ADR-040 §5)" 8 ;;
  *) die "could not check that every repository entered through a single checkout (exit $identity_rc)" ;;
esac

# --------------------------------------------------------------------------- #
# The other checkouts of the same repositories
#
# Consequence of the gate above: work living in a checkout the platform graph does
# not read is outside the artifact. That is correct -- the artifact describes one
# tree per repository -- but it must not be silent, because the checkout left out
# can be the one someone is editing right now. Surveyed, never gated: a sibling is
# not an input, and a sibling that cannot be read is reported as unreadable rather
# than stopping a run that does not depend on it.
# --------------------------------------------------------------------------- #
SIBLINGS_JSONL="$WORK/siblings.jsonl"
: > "$SIBLINGS_JSONL"
while IFS=$'\t' read -r sib_remote sib_id sib_dir sib_reason; do
  [ -n "$sib_id" ] || continue
  sib_head=""
  sib_branch=""
  sib_dirty=-1
  sib_note="$sib_reason"
  if [ -n "$sib_dir" ] && [ -e "$sib_dir/.git" ]; then
    if sib_head="$(git -C "$sib_dir" rev-parse HEAD 2> /dev/null)"; then
      sib_branch="$(git -C "$sib_dir" rev-parse --abbrev-ref HEAD 2> /dev/null)" || sib_branch=""
      if git -C "$sib_dir" status --porcelain > "$WORK/sib-status.txt" 2> /dev/null; then
        sib_dirty="$(rg -c '' "$WORK/sib-status.txt")" || sib_dirty=0
      else
        sib_note="working tree state unreadable"
      fi
    else
      sib_head=""
      sib_note="present but unreadable: git could not resolve its HEAD"
    fi
  elif [ -z "$sib_note" ]; then
    sib_note="no checkout on this machine"
  fi
  if [ "$sib_dirty" -gt 0 ]; then
    note "note: '$sib_id' is another checkout of the same repository, on branch ${sib_branch:-unknown} with $sib_dirty uncommitted change(s); the platform graph does not read it"
  fi
  python3 -c 'import json,sys
head, dirty = sys.argv[4], int(sys.argv[6])
print(json.dumps({
    "repo": sys.argv[1],
    "remote": sys.argv[2],
    "path": sys.argv[3] or None,
    "head": head or None,
    "branch": sys.argv[5] or None,
    "uncommitted_files": None if dirty < 0 else dirty,
    "note": sys.argv[7] or None,
}, ensure_ascii=False))' \
    "$sib_id" "$sib_remote" "$sib_dir" "$sib_head" "$sib_branch" "$sib_dirty" "$sib_note" \
    >> "$SIBLINGS_JSONL"
done < "$SIBLINGS_TSV"

# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #
# Recomputed: a stale auto-discovered source may have been unset from SRC_GRAPH
# above, so this can be lower than the N_SOURCES discovery started with.
N_MERGED="${#SRC_GRAPH[@]}"
if [ "$N_MERGED" -lt 2 ]; then
  die "at least two source graphs must survive discovery and staleness filtering; $N_MERGED did (see the warnings above and skipped_targets)"
fi
note "Merging $N_MERGED graphs..."
rm -f "$MERGED_TMP"
merge_rc=0
graphify merge-graphs "${SRC_GRAPH[@]}" --out "$MERGED_TMP" > "$WORK/merge.log" 2>&1 || merge_rc=$?
if [ "$merge_rc" -ne 0 ] || [ ! -f "$MERGED_TMP" ]; then
  emit_stderr "$WORK/merge.log"
  die "graphify merge-graphs failed (exit $merge_rc)" 7
fi
merge_line="$(rg -N '^Merged ' "$WORK/merge.log" || printf '')"
if [ -n "$merge_line" ]; then note "$merge_line"; fi

# --------------------------------------------------------------------------- #
# Credential scan before publishing. The merged graph carries labels lifted from
# every source file graphify parsed, `.mcp.json` included. It is scanned by shape,
# line by line, and a hit reports only the rule name and the line number, never the
# matched text: a scanner that echoes its finding is a second leak.
#
# The rules live in scripts/lib/scan-credential-shapes.py so the canary battery can
# exercise one shape per fixture without rebuilding a graph, and so this gate covers
# the prefix-less shapes too -- an opaque high-entropy value with no keyword around
# it, and `<name>=<value>` in a URL, which is the form of an `authpt` parameter.
#
# Exit 1 from the scanner means it matched. Anything else means it could not run,
# which is not a clean scan either: both refuse to publish.
#
# An exit 0 is not taken on its own either. The scanner reports how many lines it
# read, and that count is reconciled against the artifact about to be published:
# "it came back clean" and "it read the file" are two different claims, and only the
# second one makes the first worth anything.
# --------------------------------------------------------------------------- #
note "Scanning the merged graph for credential shapes..."
scan_rc=0
python3 "$CRED_SCANNER" "$MERGED_TMP" > "$WORK/scan.out" || scan_rc=$?
if [ "$scan_rc" -eq 1 ]; then
  rm -f "$MERGED_TMP"
  die "the merged graph tripped the credential scan; nothing was published" 5
elif [ "$scan_rc" -ne 0 ]; then
  rm -f "$MERGED_TMP"
  die "the credential scan could not run (exit $scan_rc); nothing was published, because an unrun scan is not a clean scan" 5
fi

scanned_lines="$(rg -N -o '^credential-scan: clean rules=[0-9]+ heuristics=[0-9]+ lines=([0-9]+) chars=[0-9]+$' -r '$1' "$WORK/scan.out" || printf '')"
if [ -z "$scanned_lines" ]; then
  rm -f "$MERGED_TMP"
  die "the credential scan exited 0 without reporting what it inspected; nothing was published, because a verdict with no units behind it is not a clean scan" 5
fi

# The scanner splits on '\n' exactly like `rg -c`, so the two counts are comparable.
graph_lines=0
lines_rc=0
graph_lines="$(rg -c '' "$MERGED_TMP")" || lines_rc=$?
case "$lines_rc" in
  0) : ;;
  1) graph_lines=0 ;;
  *) rm -f "$MERGED_TMP"
     die "could not count the lines of the merged graph to check the credential scan covered it (rg exit $lines_rc); nothing was published" 5 ;;
esac
if [ "$scanned_lines" -ne "$graph_lines" ]; then
  rm -f "$MERGED_TMP"
  die "the credential scan inspected $scanned_lines of the $graph_lines line(s) in the merged graph; nothing was published, because a partial scan is not a clean scan" 5
fi
note "credential scan clean over $scanned_lines line(s)"

# --------------------------------------------------------------------------- #
# Provenance sidecar and report
# --------------------------------------------------------------------------- #
MERGED_SHA="$(hash_of "$MERGED_TMP")"
# Provenance, not a gate: if the version cannot be read the sidecar says so out loud
# instead of pretending to know it.
GRAPHIFY_VERSION="unknown"
if graphify --version > "$WORK/version.txt" 2> /dev/null; then
  GRAPHIFY_VERSION="$(tr -d '\n' < "$WORK/version.txt")"
fi
if [ -z "$GRAPHIFY_VERSION" ]; then GRAPHIFY_VERSION="unknown"; fi
if [ "$GRAPHIFY_VERSION" = "unknown" ]; then
  note "warning: could not read the graphify version; the sidecar records 'unknown'"
fi

python3 - "$MERGED_TMP" "$SOURCES_JSON" "$SKIPPED_JSONL" "$PROVENANCE" "$REPORT" "$MERGED_SHA" "$GRAPHIFY_VERSION" "$MERGED" "$SIBLINGS_JSONL" <<'PY'
import collections, datetime, hashlib, json, os, sys

merged_tmp, sources_path, skipped_path, prov_path, report_path, merged_sha, tool_version, final_path = sys.argv[1:9]
siblings_path = sys.argv[9]

with open(sources_path, encoding="utf-8") as fh:
    sources = json.load(fh)
with open(merged_tmp, encoding="utf-8") as fh:
    merged = json.load(fh)

# One JSON object per line: every graph.targets entry (or explicit --source
# stale skip) this run did NOT fuse, with why. Never silently absent -- "I
# looked and it wasn't ready" is a real, reportable answer.
skipped = []
if os.path.isfile(skipped_path):
    with open(skipped_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                skipped.append(json.loads(line))

# The other checkouts of the repositories this artifact reads. Not inputs: the
# artifact reads one checkout per repository, and this is the list of the ones it
# deliberately left out, with the state each was in when it was left out.
siblings = []
if os.path.isfile(siblings_path):
    with open(siblings_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                siblings.append(json.loads(line))


def repo_of(node_id):
    return node_id.split("::", 1)[0] if "::" in node_id else ""


intra = collections.Counter()
cross = 0
for link in merged.get("links", []):
    a, b = repo_of(str(link.get("source"))), repo_of(str(link.get("target")))
    if a and a == b:
        intra[a] += 1
    else:
        cross += 1

nodes_per_repo = collections.Counter(repo_of(str(n.get("id"))) for n in merged.get("nodes", []))

# prefix_graph_for_global namespaces ids, not labels. Every label shared by two
# repositories is a place where graphify's label resolver can answer a query with a
# node from the wrong repository, which is why explain/query/path degrade on the
# merged graph.
label_repos = collections.defaultdict(set)
label_count = collections.Counter()
for node in merged.get("nodes", []):
    label = (node.get("label") or "").strip()
    if not label:
        continue
    label_repos[label].add(repo_of(str(node.get("id"))))
    label_count[label] += 1
shared_labels = [lab for lab, repos in label_repos.items() if len(repos) > 1]
shared_label_nodes = sum(label_count[lab] for lab in shared_labels)

# A merged graph has no single commit. The merge identity is the digest of the
# canonical (tag, built_at_commit) list, so project_cards.graph_commit can be NOT NULL
# as ADR-026 requires while staying obviously distinct from a 40-hex git sha.
canonical = "\n".join(
    "{0}:{1}".format(s["tag"], s["built_at_commit"]) for s in sorted(sources, key=lambda s: s["tag"])
)
merge_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

provenance = {
    "schema": "knowledge.platform-graph.provenance/v1",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "generator": {
        "script": "scripts/build-platform-graph.sh",
        "tool": "graphify merge-graphs",
        "tool_version": tool_version or "unknown",
    },
    "merge_id": merge_id,
    "graph_commit": merge_id,
    "graph_commit_note": (
        "merge-graphs drops built_at_commit from its output, which keeps five top-level keys "
        "only. graph_commit here is the sha256 of the sorted '<tag>:<built_at_commit>' list of "
        "the sources, so ADR-026's CHECK (graph_summary IS NULL OR graph_commit IS NOT NULL) "
        "can be satisfied for a platform card. Per-repository staleness is checked against "
        "sources[].built_at_commit, never against this digest."
    ),
    "merged": {
        "path": final_path,
        "sha256": merged_sha,
        "bytes": os.path.getsize(merged_tmp),
        "nodes": len(merged.get("nodes", [])),
        "links": len(merged.get("links", [])),
        "top_level_keys": list(merged.keys()),
        "built_at_commit_present": "built_at_commit" in merged,
        "nodes_per_repo": dict(sorted(nodes_per_repo.items())),
        "intra_repo_links": dict(sorted(intra.items())),
        "cross_repo_links": cross,
        "labels_total": len(label_repos),
        "labels_shared_across_repos": len(shared_labels),
        "nodes_with_shared_label": shared_label_nodes,
    },
    "sources": sources,
    # Every declared graph.targets entry this run did not fuse, and why: unresolved
    # on this machine, no graph built yet, or stale against its own repository's
    # HEAD without --allow-stale. An explicit --source run never populates this
    # (staleness there is a hard failure, not a skip), so an empty list here from
    # a --source run is expected, not evidence that everything was fresh.
    "skipped_targets": skipped,
    # Every other checkout of a repository fused above, with its branch and its
    # uncommitted count at merge time. The artifact reads one checkout per
    # repository, so anything living only in these trees is outside it; naming
    # them is what keeps that absence from being silent. Populated from the
    # repository registry, so an explicit --source run leaves it empty.
    "sibling_checkouts": siblings,
    "graph_targets_summary": {
        "declared": len(sources) + len(skipped),
        "fused": len(sources),
        "skipped": len(skipped),
    },
}

with open(prov_path, "w", encoding="utf-8") as fh:
    json.dump(provenance, fh, ensure_ascii=False, indent=2)
    fh.write("\n")

m = provenance["merged"]
pct = (m["nodes_with_shared_label"] / m["nodes"] * 100) if m["nodes"] else 0.0
lines = [
    "# Grafo de plataforma",
    "",
    "Generado: {0}  ".format(provenance["generated_at"]),
    "Identidad de la fusión (`merge_id`): `{0}`  ".format(merge_id[:16]),
    "Herramienta: `{0}` ({1})".format(
        provenance["generator"]["tool"], provenance["generator"]["tool_version"]
    ),
    "",
    "## Insumos",
    "",
    "| Tag | `graph.targets` | Nodos | Aristas | Rama | `built_at_commit` | Al día | Árbol limpio |",
    "|---|---|---:|---:|---|---|---|---|",
]
for s in sources:
    fresh = {True: "sí", False: "NO", None: "sin repositorio"}[s["fresh"]]
    dirty = s.get("uncommitted_files")
    if dirty is None:
        clean = "sin repositorio"
    elif dirty == 0:
        clean = "sí"
    else:
        clean = "no ({0} sin commitear)".format(dirty)
    declared = s.get("declared_target") or "— (--source explícito)"
    branch = s.get("repo_branch")
    branch_cell = "`{0}`".format(branch) if branch else "sin repositorio"
    lines.append(
        "| `{0}` | `{1}` | {2:,} | {3:,} | {4} | `{5}` | {6} | {7} |".format(
            s["tag"], declared, s["nodes"], s["links"], branch_cell,
            s["built_at_commit"][:12], fresh, clean
        )
    )

declared_total = len(sources) + len(skipped)
lines += [
    "",
    "## Objetivos de `graph.targets` sin fusionar",
    "",
    (
        "`config.json` declara {0} objetivo(s) en `graph.targets`; {1} se fusionaron arriba "
        "y {2} se omitieron:"
    ).format(declared_total, len(sources), len(skipped))
    if skipped or declared_total != len(sources)
    else "Ninguno: todo lo declarado en `graph.targets` se fusionó arriba.",
    "",
]
if skipped:
    lines += [
        "| Objetivo declarado | Motivo |",
        "|---|---|",
    ]
    for entry in skipped:
        lines.append("| `{0}` | {1} |".format(entry["target"], entry["reason"]))
    lines.append("")

lines += [
    "## Otras copias de trabajo de los mismos repositorios",
    "",
]
if siblings:
    lines += [
        (
            "Este artefacto lee cada repositorio por un solo checkout. Estas son las demás copias "
            "que el registro conoce y que el grafo **no** lee: lo que viva solo en ellas está fuera "
            "de aquí."
        ),
        "",
        "| Repositorio | Rama | Sin commitear | Notas del censo |",
        "|---|---|---:|---|",
    ]
    for sib in siblings:
        dirty = sib.get("uncommitted_files")
        lines.append(
            "| `{0}` | {1} | {2} | {3} |".format(
                sib["repo"],
                "`{0}`".format(sib["branch"]) if sib.get("branch") else "sin rama",
                "sin medir" if dirty is None else "{0:,}".format(dirty),
                sib.get("note") or "sin novedad",
            )
        )
    lines.append("")
else:
    lines += [
        (
            "Ninguna: ningún repositorio fusionado tiene otra copia de trabajo declarada en el "
            "registro. Una corrida con `--source` explícito no consulta el registro y también "
            "deja esta sección vacía."
        ),
        "",
    ]

lines += [
    "## Resultado",
    "",
    "- Nodos: **{0:,}**".format(m["nodes"]),
    "- Aristas: **{0:,}**".format(m["links"]),
    "- Aristas entre repositorios: **{0}**".format(m["cross_repo_links"]),
    "- Tamaño: {0:.1f} MiB".format(m["bytes"] / (1024 * 1024)),
    "- Claves de primer nivel: {0}".format(", ".join("`" + k + "`" for k in m["top_level_keys"])),
    "- `built_at_commit` en el archivo fusionado: {0}".format(
        "sí" if m["built_at_commit_present"] else "no; para eso existe el sidecar de procedencia"
    ),
    "",
    "## Límites medidos",
    "",
    "`merge-graphs` no infiere una sola arista entre repositorios: las {0:,} aristas del grafo".format(m["links"]),
    "fusionado son todas internas de un insumo. El grafo de plataforma es, literalmente, un conjunto",
    "de componentes desconectados, uno por insumo, y por eso `graphify path` entre dos servicios",
    "siempre responde «No path found».",
    "",
    "`prefix_graph_for_global` agrega el prefijo a los identificadores, no a las etiquetas:",
    "{0:,} etiquetas aparecen en dos o más insumos y afectan a {1:,} nodos ({2:.1f} % del grafo).".format(
        m["labels_shared_across_repos"], m["nodes_with_shared_label"], pct
    ),
    "El resolvedor de etiquetas de `explain`, `query` y `path` puede contestar con un nodo del",
    "repositorio equivocado, así que este artefacto sirve para inventario y métricas de plataforma,",
    "no para navegación simbólica: para eso hay que seguir usando el grafo de cada repositorio.",
    "",
]
with open(report_path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))

print(json.dumps({
    "nodes": m["nodes"],
    "links": m["links"],
    "cross_repo_links": m["cross_repo_links"],
    "labels_shared_across_repos": m["labels_shared_across_repos"],
}))
PY

mv "$MERGED_TMP" "$MERGED"

note ""
note "OK: $MERGED"
note "    $PROVENANCE"
note "    $REPORT"
