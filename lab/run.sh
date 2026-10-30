#!/usr/bin/env bash
# Run the lab end to end: seed the fake tenants and their clients, start two
# server instances, wait for both, run the server checks (over HTTP) and the
# client checks (`cloudfrog-knowledge init` on the host) and print a PASS/FAIL
# table for each.
#
#   lab/run.sh           seed, build, start, check; leaves the instances running
#   lab/run.sh --check   run only the checks against the running instances
#   lab/run.sh --down    remove containers, volumes, networks and lab/.work
#
# Needs docker (compose v2), git, curl and uv. The exit status is the one of
# the checks: 0 only when every row of both tables is PASS.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
work="$here/.work"
envfile="$work/lab.env"

compose() {
  docker compose --env-file "$envfile" -f "$here/compose.yml" "$@"
}

# The bearers are generated once per lab and kept in lab/.work/lab.env, which is
# gitignored; nothing secret-looking is committed.
ensure_env() {
  mkdir -p "$work"
  [ -f "$envfile" ] && return 0
  {
    echo "LAB_ACME_PORT=${LAB_ACME_PORT:-18101}"
    echo "LAB_GLOBEX_PORT=${LAB_GLOBEX_PORT:-18102}"
    for tenant in ACME GLOBEX; do
      echo "LAB_${tenant}_READ_KEY=$(openssl rand -hex 16)"
      echo "LAB_${tenant}_ADMIN_KEY=$(openssl rand -hex 16)"
      echo "LAB_${tenant}_WEBHOOK_SECRET=$(openssl rand -hex 16)"
    done
  } > "$envfile"
  chmod 600 "$envfile"
}

port_of() {
  sed -n "s/^$1=//p" "$envfile"
}

wait_healthy() {
  local name="$1" port="$2"
  for _ in $(seq 1 90); do
    if curl -fsS "http://127.0.0.1:$port/health" > /dev/null 2>&1; then
      echo "up: $name on 127.0.0.1:$port"
      return 0
    fi
    sleep 2
  done
  echo "timeout waiting for $name on 127.0.0.1:$port" >&2
  compose logs --tail 60 "$name" >&2 || true
  return 1
}

run_checks() {
  local status=0
  (cd "$root" && uv run python lab/check.py) || status=1
  (cd "$root" && uv run python lab/check_clients.py) || status=1
  return $status
}

ensure_env

case "${1:-}" in
  --down)
    compose down -v --remove-orphans
    rm -rf "$work"
    echo "lab removed"
    exit 0
    ;;
  --check)
    run_checks
    exit $?
    ;;
  "") ;;
  *)
    echo "usage: lab/run.sh [--check|--down]" >&2
    exit 2
    ;;
esac

# Start from nothing: a bind mount of a folder that seed.sh rebuilds would
# otherwise keep pointing at the deleted one.
compose down -v --remove-orphans > /dev/null 2>&1 || true
"$here/seed.sh"
compose up -d --build

wait_healthy knowledge-acme "$(port_of LAB_ACME_PORT)"
wait_healthy knowledge-globex "$(port_of LAB_GLOBEX_PORT)"

run_checks
