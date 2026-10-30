#!/usr/bin/env bash
# .claude/hooks/block-secrets.sh — PreToolUse on Edit|Write: refuse content that looks like a credential.
#
# Reads the tool-call JSON from stdin, extracts the content about to be written,
# and blocks (exit 2) when it looks like a credential. Prefers gitleaks when it
# is installed; otherwise falls back to a conservative built-in pattern set so
# the gate still works on a machine without gitleaks.
#
# Ring 0 (ADR-006): local and advisory. It stops an accidental paste inside
# Claude Code; it is not a substitute for a scanner on the merge path.
set -euo pipefail

payload="$(cat)"
content="$(printf '%s' "$payload" | jq -r '.tool_input.content // .tool_input.new_string // empty' 2>/dev/null || true)"

[ -z "$content" ] && exit 0

if command -v gitleaks >/dev/null 2>&1; then
  if printf '%s' "$content" | gitleaks detect --pipe --redact --exit-code 1 --no-banner >/dev/null 2>&1; then
    exit 0
  fi
  echo "block-secrets: gitleaks flagged this content as a credential; move it to an environment variable or a secret store" >&2
  exit 2
fi

echo "block-secrets: gitleaks not found on this machine, using the built-in pattern set only (install: brew install gitleaks)" >&2

# Conservative set: cloud provider keys, common VCS/chat tokens, PEM private key
# headers, JWT-shaped triplets, and generic name=value / name: value secret
# assignments.
#
# The generic rule allows an alphanumeric suffix between the keyword and the
# separator, so AWS_SECRET_ACCESS_KEY=... is caught and not just SECRET=...;
# the 12-character minimum on the value is what keeps `token_count = 42` and
# similar prose out of the results. The value may not start with an
# interpolation, so `api_key: ${OPENAI_API_KEY}` -- the practice this gate
# exists to encourage -- is never flagged as the secret it points at.
PATTERN='AKIA[0-9A-Z]{16}'
PATTERN="$PATTERN|gh[pousr]_[A-Za-z0-9]{36}"
PATTERN="$PATTERN|xox[baprs]-[0-9A-Za-z-]{10,}"
PATTERN="$PATTERN|AIza[0-9A-Za-z_-]{35}"
PATTERN="$PATTERN|sk-(proj-)?[A-Za-z0-9_-]{20,}"
PATTERN="$PATTERN|-----BEGIN[ A-Z]*PRIVATE KEY-----"
PATTERN="$PATTERN|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"
PATTERN="$PATTERN|(secret|token|api[_-]?key|password|passwd|pwd)[A-Za-z0-9_-]{0,24}[ \t]{0,2}[:=][ \t]{0,2}[\"'\`]?[A-Za-z0-9/+_.=-]{12,}"

if printf '%s' "$content" | rg -qiP "$PATTERN" 2>/dev/null; then
  echo "block-secrets: the content matches a known credential pattern; move it to an environment variable or a secret store" >&2
  exit 2
fi

exit 0
