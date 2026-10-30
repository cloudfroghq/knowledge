#!/usr/bin/env bash
# .claude/hooks/remind-mem-save.sh — Stop: non-blocking reminder to persist
# decisions, root causes and smoke results before the session closes.
set -euo pipefail
printf '%s\n' "{\"systemMessage\": \"Before closing: mem_save decisions, root causes and smoke results (topic keys sdd/<change>/state, incident/<KEY>, knowledge/reindex/<date>). Never include API keys or bearer tokens.\"}"
exit 0
