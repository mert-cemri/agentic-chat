#!/usr/bin/env bash
# check-relay.sh -- lightweight unread-check for Claude Code UserPromptSubmit hook.
# Outputs JSON with additionalContext if there are unread messages on the relay.
# Designed to fail silently and never block Claude Code.
#
# Required env vars: RELAY_URL, RELAY_TOKEN

set -euo pipefail

# ── Fail silently on any error ──────────────────────────────────────
trap 'exit 0' ERR

# ── Required env ────────────────────────────────────────────────────
RELAY_URL="${RELAY_URL:-}"
RELAY_TOKEN="${RELAY_TOKEN:-}"

if [[ -z "$RELAY_URL" || -z "$RELAY_TOKEN" ]]; then
    exit 0
fi

# ── Throttle: at most once per 30 seconds ───────────────────────────
STAMP_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/claude-relay-check-ts"
mkdir -p "$(dirname "$STAMP_FILE")"

if [[ -f "$STAMP_FILE" ]]; then
    last_check=$(cat "$STAMP_FILE" 2>/dev/null || echo 0)
    now=$(date +%s)
    if (( now - last_check < 30 )); then
        exit 0
    fi
fi
date +%s > "$STAMP_FILE"

# ── Call the dashboard API (3-second timeout) ───────────────────────
response=$(curl -s --max-time 3 \
    -H "Authorization: Bearer ${RELAY_TOKEN}" \
    "${RELAY_URL}/dashboard/api/status" 2>/dev/null) || exit 0

# ── Parse unread count ──────────────────────────────────────────────
# The status endpoint returns JSON with total_unread.
total_unread=$(echo "$response" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('total_unread', 0))
except Exception:
    print(0)
" 2>/dev/null) || exit 0

if [[ "$total_unread" -gt 0 ]]; then
    cat <<ENDJSON
{
  "additionalContext": "RELAY NOTIFICATION: You have ${total_unread} unread message(s) on the relay. Let the user know and offer to read them."
}
ENDJSON
fi
