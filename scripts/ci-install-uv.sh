#!/bin/sh
# Install the uv release the npm wrapper pins, verified, into DIR.
#
# Usage: scripts/ci-install-uv.sh DIR
#
# Reads the version and the SHA-256 of this machine's archive from
# npm/lib/uv-release.json (generated from the release's .sha256 files by
# npm/scripts/uv-release.js), downloads the archive from the uv release on
# GitHub, refuses it when the hash differs, extracts uv and uvx into DIR and,
# inside a GitHub Actions job, appends DIR to $GITHUB_PATH.
#
# The publish workflow uses it because its jobs can mint the OIDC tokens that
# PyPI and npm accept: the installer script ring1 runs
# (astral.sh/uv/<version>/install.sh) downloads the same archive without
# checking any hash. Exit status: 0 installed, 1 download or hash failure,
# 2 on a usage error or an unsupported runner.

set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: scripts/ci-install-uv.sh DIR" >&2
  exit 2
fi
dest=$1
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) triple=x86_64-unknown-linux-gnu ;;
  Linux-aarch64 | Linux-arm64) triple=aarch64-unknown-linux-gnu ;;
  Darwin-arm64) triple=aarch64-apple-darwin ;;
  Darwin-x86_64) triple=x86_64-apple-darwin ;;
  *)
    echo "ci-install-uv: no pinned uv archive for $(uname -s)-$(uname -m)" >&2
    exit 2
    ;;
esac

# shellcheck disable=SC2046 # three words, none with spaces
set -- $(python3 - "$root/npm/lib/uv-release.json" "$triple" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    release = json.load(handle)
asset = release["assets"][sys.argv[2]]
print(release["version"], asset["file"], asset["sha256"])
PY
)
version=$1
file=$2
expected=$3

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL --retry 3 -o "$work/$file" "https://github.com/astral-sh/uv/releases/download/$version/$file"
actual=$(python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$work/$file")
if [ "$actual" != "$expected" ]; then
  echo "ci-install-uv: $file has SHA-256 $actual, expected $expected" >&2
  exit 1
fi

mkdir -p "$dest"
tar -xzf "$work/$file" -C "$dest" --strip-components=1
"$dest/uv" --version
if [ -n "${GITHUB_PATH:-}" ]; then
  echo "$dest" >>"$GITHUB_PATH"
fi
