#!/bin/sh
# Container entrypoint: make the vaults exist, prepare the indexes, then start
# the server. With arguments, the arguments run instead of the last two steps.
#
# Three ways to get vaults into the container, chosen by two variables:
#
#   KNOWLEDGE_VAULTS_YAML set
#                       the content of a vaults.yaml, any number of vaults.
#                       Each entry is a git vault (`repo`, optionally
#                       `git.branch`): cloned or refreshed into its `path`, or
#                       into /data/vaults/<slug> when it names none. Or a
#                       folder vault (`path` only): an already mounted
#                       directory, used as it is, git never runs for it. The
#                       resolved file is handed to the server through
#                       KNOWLEDGE_VAULTS. See infra/resolve-vaults.py.
#   VAULT_REMOTE set    one vault, a git clone kept in VAULT_CLONE_DIR. The
#                       clone is created or adopted here, read with the
#                       organization's own deploy key.
#   VAULT_REMOTE empty  one vault, whatever the volume already holds. Git is
#                       never invoked.
#
# A git vault read over ssh needs a deploy key (base64 of the private key),
# taken from the first of these that is set: the `deploy_key_env` of the entry,
# <SLUG_UPPER>_GIT_DEPLOY_KEY_B64 and GIT_DEPLOY_KEY_B64. file://, http(s):// and
# local-path remotes need none. Each vault gets its own key file and the clone
# remembers it (core.sshCommand), so the server's later `git fetch` uses the same
# key.
#
# Nothing here prints a secret: a deploy key only travels through a pipe into
# a 0600 file.
set -eu

clone_dir="${VAULT_CLONE_DIR:-/data/vault-repo}"
branch="${VAULT_BRANCH:-main}"
ssh_dir="${HOME:-/root}/.ssh"
tab="$(printf '\t')"

# Credentials embedded in a URL (user:password@ on any scheme, any user@ on http(s)) never reach
# a log or an error: git prints the remote it was given when it fails, and so do we.
redact() {
  sed -e 's#\([A-Za-z][A-Za-z0-9+.-]*://\)[^/@: ]*:[^/@ ]*@#\1***@#g' \
      -e 's#\(https\{0,1\}://\)[^/@ ]*@#\1***@#g'
}

# Host of an scp-like (git@host:path) or ssh:// remote; github.com otherwise.
remote_host() {
  printf '%s' "$1" | sed -n -e 's#^[a-z+]*://\([^@/]*@\)\{0,1\}\([^:/]*\).*#\2#p' -e 's#^[^@/]*@\([^:/]*\):.*#\1#p' | head -n 1
}

# Clone or refresh one git vault of KNOWLEDGE_VAULTS_YAML. Arguments: slug,
# remote, branch, destination, name of the variable that holds the key ("-"
# when the remote needs none).
sync_git_vault() {
  slug="$1"; repo="$2"; vbranch="$3"; dest="$4"; keyvar="$5"
  ssh_cmd=""
  if [ "$keyvar" != "-" ]; then
    mkdir -p "$ssh_dir"
    keyfile="$ssh_dir/knowledge_$slug"
    ( umask 077; printenv "$keyvar" | base64 -d > "$keyfile" )
    chmod 600 "$keyfile"
    ssh_cmd="ssh -i $keyfile -o IdentitiesOnly=yes"
    host="$(remote_host "$repo")"
    ssh-keyscan "${host:-github.com}" >> "$ssh_dir/known_hosts" 2>/dev/null || true
  fi

  # The volume outlives the container that created it, and a bind-mounted
  # origin belongs to whoever created it: git refuses both as "dubious
  # ownership" unless they are listed.
  git config --global --add safe.directory "$dest"
  case "$repo" in
    file://*) git config --global --add safe.directory "${repo#file://}" ;;
  esac

  if [ -d "$dest/.git" ]; then
    # An existing clone is adopted. A refresh that fails (the remote is down)
    # leaves the vault as it is on disk: one unreachable remote does not take
    # the other vaults down with it.
    if ! git_with_key git -C "$dest" remote set-url origin "$repo" \
      || ! git_with_key git -C "$dest" fetch --prune origin \
      || ! git_with_key git -C "$dest" checkout -f -B "$vbranch" "origin/$vbranch"; then
      echo "W_VAULT_REFRESH_FAILED: $slug could not be refreshed from its remote; serving what is on disk" >&2
    fi
  elif ! git_with_key git clone --branch "$vbranch" -- "$repo" "$dest"; then
    echo "E_VAULT_CLONE_FAILED: $slug could not be cloned from $(printf '%s' "$repo" | redact) (branch $vbranch) into $dest" >&2
    exit 1
  fi

  # Pin the key to the clone so the `git fetch` run later by the server reads the
  # same remote with the same key.
  if [ -n "$ssh_cmd" ]; then
    git -C "$dest" config core.sshCommand "$ssh_cmd"
  fi
}

# Run a git command with the key of the vault being synced, if it has one. What git
# says on stderr goes through redact: a failure quotes the remote, credentials and all.
git_with_key() {
  errfile="$(mktemp)"
  status=0
  if [ -n "$ssh_cmd" ]; then
    GIT_SSH_COMMAND="$ssh_cmd" "$@" 2>"$errfile" || status=$?
  else
    "$@" 2>"$errfile" || status=$?
  fi
  redact < "$errfile" >&2
  rm -f "$errfile"
  return "$status"
}

multi=""
if [ -n "${KNOWLEDGE_VAULTS_YAML:-}" ]; then
  multi=1
  resolver="${KNOWLEDGE_RESOLVER:-/usr/local/bin/knowledge-resolve-vaults}"
  resolved="${KNOWLEDGE_VAULTS_RESOLVED:-/data/vaults.resolved.yaml}"

  # A failing resolver stops the start with its coded line on stderr.
  plan="$(python "$resolver" "$resolved")"
  if [ -n "$plan" ]; then
    # stdin is closed for the sync itself: ssh would otherwise read the rest
    # of the plan from the loop's input.
    while IFS="$tab" read -r slug repo vbranch dest keyvar; do
      sync_git_vault "$slug" "$repo" "$vbranch" "$dest" "$keyvar" </dev/null
    done <<EOF
$plan
EOF
  fi

  # The registry file wins over the single-vault variables, which the image
  # sets by default and which would otherwise be reported as ignored.
  KNOWLEDGE_VAULTS="$resolved"
  export KNOWLEDGE_VAULTS
  unset VAULT_PATH VAULT_REMOTE
elif [ -n "${VAULT_REMOTE:-}" ]; then
  if [ -z "${GIT_DEPLOY_KEY_B64:-}" ]; then
    echo "E_DEPLOY_KEY_MISSING: VAULT_REMOTE is set but GIT_DEPLOY_KEY_B64 is empty" >&2
    exit 64
  fi

  mkdir -p "$ssh_dir"
  printf '%s' "$GIT_DEPLOY_KEY_B64" | base64 -d > "$ssh_dir/id_ed25519"
  chmod 600 "$ssh_dir/id_ed25519"

  host="$(remote_host "$VAULT_REMOTE")"
  ssh-keyscan "${host:-github.com}" >> "$ssh_dir/known_hosts" 2>/dev/null || true

  # The volume outlives the container that created it, so git may see a
  # worktree owned by someone else.
  git config --global --add safe.directory "$clone_dir"

  if [ -d "$clone_dir/.git" ]; then
    # An existing clone is adopted: repoint the remote and force the worktree
    # onto the branch, which is what brings the new content in. The webhook
    # realigns a clone whose branch was rewritten with the same fetch and
    # checkout (knowledge.gitsync.converge); keep the two in step.
    git -C "$clone_dir" remote set-url origin "$VAULT_REMOTE"
    git -C "$clone_dir" fetch --prune origin
    git -C "$clone_dir" checkout -f -B "$branch" "origin/$branch"
  else
    git clone --branch "$branch" "$VAULT_REMOTE" "$clone_dir"
  fi
fi

if [ -z "$multi" ]; then
  if [ -z "${VAULT_PATH:-}" ]; then
    echo "E_VAULT_PATH_MISSING: VAULT_PATH is not set" >&2
    exit 1
  fi

  if [ ! -d "$VAULT_PATH" ]; then
    if [ -n "${VAULT_REMOTE:-}" ]; then
      echo "E_VAULT_PATH_NOT_DIR: VAULT_PATH $VAULT_PATH is not a directory in the clone of $VAULT_REMOTE (branch $branch) in $clone_dir" >&2
    else
      echo "E_VAULT_PATH_NOT_DIR: VAULT_PATH $VAULT_PATH is not a directory; mount the vault volume there or set VAULT_REMOTE" >&2
    fi
    exit 1
  fi
fi

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# Bring each vault's index directory to the current layout before serving.
# migrate-index embeds nothing: it creates index.sqlite and, when a Chroma
# directory is found, copies its usage statistics and learned link weights.
# The index fills on the first tool call or webhook, so the healthcheck's start
# period is never spent embedding. An index written by a newer package
# (E_INDEX_VERSION_NEWER) or a file SQLite cannot read (E_INDEX_UNREADABLE)
# stops the start here, with one coded line and no traceback. Its report goes
# to stderr: on the stdio transport stdout belongs to the protocol.
python -m knowledge migrate-index >&2
exec python -m knowledge serve
