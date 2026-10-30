#!/usr/bin/env bash
# Build the fake tenants and the fake clients of the lab under lab/.work/ (gitignored).
#
# Tenants (what the servers serve):
#   acme    two vaults: acme-handbook (a bare git repo served over file://) and
#           acme-ops (a plain folder, no git)
#   globex  two vaults: globex-docs (a bare git repo served over file://) and
#           globex-notes (a plain folder whose Contexts/config.json declares no apps)
#
# Each vault has a vault.yaml, three notes (clean for vault-lint 1.3) with the
# services map they need, a "90 - Memory" folder, a .gitignore and a Contexts/
# folder: a config.json that declares fake applications and one context package
# per application. Every tenant owns words the others never use
# (zephyr, quokka, ...), so isolation is checkable by searching them; every
# package owns one word in its CLAUDE.md and another in .claude/agents/x.md.
#
# Clients (a person with a vault cloned locally, talking to a remote server):
#   alice   acme: HOME is lab/.work/clients/alice, with acme-handbook cloned under
#           Vaults/, acme-ops pointed at its folder, acme-web and acme-deploy
#           checked out under Projects/ (acme-api is not) and a synthetic tree of
#           about 2,000 directories, 60 of them git checkouts, to time the scan
#   bob     globex: globex-docs cloned under Vaults/, globex-notes pointed at its
#           folder (it declares no apps) and globex-core checked out
#
# Usage:
#   seed.sh            rebuild the tenants, then the clients
#   seed.sh --clients  rebuild only the clients (their init state starts over)
#
# Safe to run again: everything it owns is rebuilt from scratch.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$here/.work"

# write_vault DIR SLUG NAME TENANT_WORD VAULT_WORD APP_SPEC...
# APP_SPEC is ID|ROOT|PATH|REMOTE|CLAUDE_WORD|AGENT_WORD.
write_vault() {
  local dir="$1" slug="$2" name="$3" tenant_word="$4" vault_word="$5"
  shift 5

  mkdir -p "$dir/Architecture" "$dir/Runbooks" "$dir/Services" "$dir/90 - Memory" "$dir/Contexts"

  cat > "$dir/vault.yaml" <<EOF
slug: $slug
name: $name
knowledge_contract: ">=2.0,<3"
review:
  thresholds_days:
    runbook: 90
    default: 365
memory:
  export_folder: "90 - Memory"
index:
  exclude:
    - "Contexts/**"
EOF

  cat > "$dir/.gitignore" <<EOF
Contexts/*/.claude/settings.local.json
Contexts/*/.claude/agent-memory/
Contexts/*/.claude/worktrees/
EOF

  cat > "$dir/Architecture/$name Overview.md" <<EOF
---
title: $name Overview
type: architecture
service: $slug
tags:
  - type/architecture
status: active
last_updated: 2026-10-01
---

# $name Overview

The $name platform is built around the $tenant_word pipeline.
Every request crosses the $tenant_word gateway before it reaches the $vault_word store.
The $vault_word store keeps the state that the $tenant_word gateway never owns.

See [[$name Service]] for the deployable unit and [[$name Runbook]] for operations.
EOF

  cat > "$dir/Runbooks/$name Runbook.md" <<EOF
---
title: $name Runbook
id: RB-001
type: runbook
service: $slug
tags:
  - type/runbook
status: verified
last_updated: 2026-10-01
---

# $name Runbook

1. Check that the $tenant_word gateway answers its health probe.
2. Check that the $vault_word store accepted the last write.
3. Restart the $tenant_word gateway only after the $vault_word store is healthy.
EOF

  cat > "$dir/Services/$name Service.md" <<EOF
---
title: $name Service
type: service
service: $slug
tags:
  - service/$slug
status: active
last_updated: 2026-10-01
---

# $name Service

The $name service exposes the $tenant_word gateway on one port and persists to the $vault_word store.
Operational steps live in [[$name Runbook]].
EOF

  cat > "$dir/Runbooks/services.json" <<EOF
{ "services": [ { "slug": "$slug", "aliases": [] } ] }
EOF

  : > "$dir/90 - Memory/.gitkeep"
  write_contexts "$dir/Contexts" "$@"
}

# write_contexts DIR [APP_SPEC...]
# Fake context packages in the shape of Contexts/<app>, plus the config.json
# whose `repos` section declares the checkouts they belong to: one root per
# distinct ROOT, one entry per app. No root declares a default, so a client
# finds its checkouts by the origin remote alone. Without any APP_SPEC the
# registry is the empty one a vault with no apps declares (roots [], entries []).
write_contexts() {
  local dir="$1"
  shift
  local entries="" roots="" seen=" " spec id root path remote claude_word agent_word
  for spec in "$@"; do
    IFS='|' read -r id root path remote claude_word agent_word <<< "$spec"
    mkdir -p "$dir/$id/.claude/agents"
    cat > "$dir/$id/CLAUDE.md" <<EOF
# $id

Fake context package of the lab. It stands for the instructions a real checkout materializes.
The $id package teaches the $claude_word convention.
EOF
    cat > "$dir/$id/.claude/agents/x.md" <<EOF
---
name: x
description: Placeholder agent shipped with the $id context package.
---

You are a placeholder agent of the lab. Your only routine is the $agent_word routine.
EOF
    cat > "$dir/$id/.mcp.json" <<EOF
{
  "mcpServers": {
    "knowledge": {
      "type": "http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
EOF
    case "$seen" in
      *" $root "*) ;;
      *)
        seen="$seen$root "
        roots="$roots${roots:+,}
      {
        \"id\": \"$root\",
        \"description\": \"Directory that holds the $root checkouts on a person's machine.\"
      }"
        ;;
    esac
    entries="$entries${entries:+,}
      {
        \"id\": \"$id\",
        \"context\": \"$id\",
        \"install_mode\": \"link\",
        \"remote\": \"$remote\",
        \"group\": \"standalone\",
        \"root\": \"$root\",
        \"path\": \"$path\",
        \"description\": \"Fake application $id of the lab.\"
      }"
  done

  cat > "$dir/config.json" <<EOF
{
  "repos": {
    "_README": [
      "Fake checkouts declared by a lab vault. The shape is the one of the",
      "repos section of the server's config.json: roots, groups and entries."
    ],
    "roots": [$roots
    ],
    "groups": {
      "standalone": "An independent clone. Nothing else brings it down."
    },
    "entries": [$entries
    ]
  },
  "graph": {
    "targets": []
  }
}
EOF
}

# publish_repo SLUG: commit the staged vault and push it to a bare repository.
# The stage also holds what Claude Code writes next to a package through a linked
# .claude (settings.local.json, agent-memory/, worktrees/); the vault's .gitignore
# keeps it out of the commit.
publish_repo() {
  local slug="$1" stage="$work/stage/$1" bare="$work/repos/$1.git" package
  for package in "$stage"/Contexts/*/; do
    printf '{ "permissions": { "allow": [] } }\n' > "${package}.claude/settings.local.json"
    mkdir -p "${package}.claude/agent-memory/x" "${package}.claude/worktrees/w1"
    printf 'what the agent learned\n' > "${package}.claude/agent-memory/x/MEMORY.md"
    printf 'a git worktree\n' > "${package}.claude/worktrees/w1/file.txt"
  done
  git init -q --bare -b main "$bare"
  git -C "$stage" init -q -b main
  git -C "$stage" add -A
  git -C "$stage" -c user.name=lab -c user.email=lab@example.invalid commit -q -m "docs: seed $slug"
  git -C "$stage" remote add origin "$bare"
  git -C "$stage" push -q origin main
}

# add_review_fixtures DIR APP: DIR declares the governance extension (get_stale_docs) and
# holds two notes that are long overdue: one in the vault proper, which the review queue
# lists, and one inside the context package APP, which index.exclude keeps off the queue.
add_review_fixtures() {
  local dir="$1" app="$2" slug
  slug="$(basename "$dir")"
  printf 'features:\n  - governance\n' >> "$dir/vault.yaml"
  cat > "$dir/Architecture/Legacy Notes.md" <<EOF
---
title: Legacy Notes
type: architecture
service: $slug
tags:
  - type/architecture
status: active
last_updated: 2024-01-01
---

# Legacy Notes

Notes nobody has revalidated for a long time.
EOF
  cat > "$dir/Contexts/$app/history.md" <<EOF
---
title: $app history
type: note
status: active
last_updated: 2020-01-01
---

# $app history

Old notes that ship with the context package.
EOF
}

seed_servers() {
  rm -rf "$work/repos" "$work/vaults" "$work/stage"
  mkdir -p "$work/repos" "$work/vaults" "$work/stage"

  write_vault "$work/stage/acme-handbook" acme-handbook "Acme Handbook" zephyr marmot \
    "acme-web|acme|web|acme-corp/acme-web|heron|osprey" \
    "acme-api|acme|services/api|acme-corp/acme-api|ibis|kestrel"
  add_review_fixtures "$work/stage/acme-handbook" acme-web
  write_vault "$work/vaults/acme-ops" acme-ops "Acme Ops" zephyr nautilus \
    "acme-deploy|acme-infra|deploy|acme-corp/acme-deploy|plover|merlin"
  write_vault "$work/stage/globex-docs" globex-docs "Globex Docs" quokka pangolin \
    "globex-core|globex|core|globex-inc/globex-core|curlew|harrier"

  write_vault "$work/vaults/globex-notes" globex-notes "Globex Notes" quokka lemur

  publish_repo acme-handbook
  publish_repo globex-docs
  rm -rf "$work/stage"

  echo "seeded: $work"
  echo "  repos/acme-handbook.git  repos/globex-docs.git  vaults/acme-ops/  vaults/globex-notes/"
}

# new_checkout DIR REMOTE: a git checkout with one commit and an origin that
# names REMOTE, the way a clone of the application would.
new_checkout() {
  local dir="$1" remote="$2"
  git init -q -b main "$dir"
  git -C "$dir" remote add origin "git@git.lab.invalid:$remote.git"
  printf '# %s\n' "$remote" > "$dir/README.md"
  git -C "$dir" add README.md
  git -C "$dir" -c user.name=lab -c user.email=lab@example.invalid commit -q -m "docs: seed"
}

# make_noise DIR: 2,011 directories (10 groups x 20 projects x 9 folders, and the
# levels above them) down to depth 4, 60 of the projects being git checkouts
# with an origin that matches nothing a vault declares. This is the tree a
# scan for checkouts has to walk on a developer machine.
make_noise() {
  local base="$1" g p
  mkdir -p "$base"/g{0..9}/p{0..19}/s{1..9}
  for g in 0 1 2 3 4 5; do
    for p in 0 1 2 3 4 5 6 7 8 9; do
      git init -q --template= -b main "$base/g$g/p$p"
      git -C "$base/g$g/p$p" remote add origin "git@git.lab.invalid:noise/g$g-p$p.git"
    done
  done
}

# write_client_vaults HOME SLUG=DIR...: the vaults.yaml a person keeps in
# $XDG_CONFIG_HOME/knowledge/, the first vault being the default.
write_client_vaults() {
  local home="$1" pair slug dir first=1
  shift
  mkdir -p "$home/.config/knowledge"
  {
    echo "version: 1"
    echo "vaults:"
    for pair in "$@"; do
      slug="${pair%%=*}"
      dir="${pair#*=}"
      echo "  - slug: $slug"
      echo "    path: $dir"
      [ "$first" = 1 ] && echo "    default: true"
      first=0
    done
  } > "$home/.config/knowledge/vaults.yaml"
}

seed_clients() {
  [ -d "$work/repos/acme-handbook.git" ] || { echo "run lab/seed.sh before --clients" >&2; exit 1; }
  rm -rf "$work/clients"
  local alice="$work/clients/alice" bob="$work/clients/bob"
  mkdir -p "$alice/Vaults" "$alice/Projects" "$bob/Vaults" "$bob/Projects"

  git clone -q "$work/repos/acme-handbook.git" "$alice/Vaults/acme-handbook"
  mkdir -p "$alice/Projects/acme" "$alice/Projects/acme-infra"
  new_checkout "$alice/Projects/acme/web" acme-corp/acme-web
  new_checkout "$alice/Projects/acme-infra/deploy" acme-corp/acme-deploy
  make_noise "$alice/Projects/noise"
  write_client_vaults "$alice" "acme-handbook=$alice/Vaults/acme-handbook" "acme-ops=$work/vaults/acme-ops"

  git clone -q "$work/repos/globex-docs.git" "$bob/Vaults/globex-docs"
  mkdir -p "$bob/Projects/globex"
  new_checkout "$bob/Projects/globex/core" globex-inc/globex-core
  write_client_vaults "$bob" "globex-docs=$bob/Vaults/globex-docs" "globex-notes=$work/vaults/globex-notes"

  echo "seeded clients: $work/clients/{alice,bob}"
}

case "${1:-}" in
  "")
    seed_servers
    seed_clients
    ;;
  --clients)
    seed_clients
    ;;
  *)
    echo "usage: lab/seed.sh [--clients]" >&2
    exit 2
    ;;
esac
