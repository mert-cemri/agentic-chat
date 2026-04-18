#!/usr/bin/env bash
# setup-hooks.sh -- install the relay notification hook into Claude Code.
#
# Usage:
#   RELAY_URL=https://your-relay.example.com RELAY_TOKEN=tok_xxx ./setup-hooks.sh
#
# What it does:
#   1. Copies check-relay.sh to ~/.claude/
#   2. Merges a UserPromptSubmit hook into ~/.claude/settings.json
#   3. Writes RELAY_URL and RELAY_TOKEN to a small env file sourced by the hook

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="${HOME}/.claude"
SETTINGS_FILE="${CLAUDE_DIR}/settings.json"
ENV_FILE="${CLAUDE_DIR}/relay-env.sh"

# ── Validate inputs ────────────────────────────────────────────────
RELAY_URL="${RELAY_URL:-}"
RELAY_TOKEN="${RELAY_TOKEN:-}"

if [[ -z "$RELAY_URL" || -z "$RELAY_TOKEN" ]]; then
    echo "Error: RELAY_URL and RELAY_TOKEN must be set."
    echo "Usage: RELAY_URL=https://... RELAY_TOKEN=tok_xxx $0"
    exit 1
fi

# ── Ensure ~/.claude exists ─────────────────────────────────────────
mkdir -p "$CLAUDE_DIR"

# ── Copy the check script ──────────────────────────────────────────
cp "${SCRIPT_DIR}/check-relay.sh" "${CLAUDE_DIR}/check-relay.sh"
chmod +x "${CLAUDE_DIR}/check-relay.sh"
echo "Copied check-relay.sh to ${CLAUDE_DIR}/"

# ── Write env file ──────────────────────────────────────────────────
cat > "$ENV_FILE" <<EOF
export RELAY_URL="${RELAY_URL}"
export RELAY_TOKEN="${RELAY_TOKEN}"
EOF
chmod 600 "$ENV_FILE"
echo "Wrote relay credentials to ${ENV_FILE}"

# ── Build the hook command ──────────────────────────────────────────
HOOK_CMD="source ${ENV_FILE} && bash ${CLAUDE_DIR}/check-relay.sh"

# ── Merge hook into settings.json ──────────────────────────────────
python3 <<PYEOF
import json, os, sys

settings_path = "${SETTINGS_FILE}"

# Load existing settings or start fresh
if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
else:
    settings = {}

# Ensure hooks structure exists
if "hooks" not in settings:
    settings["hooks"] = {}
if "UserPromptSubmit" not in settings["hooks"]:
    settings["hooks"]["UserPromptSubmit"] = []

hook_entry = {
    "type": "command",
    "command": "${HOOK_CMD}"
}

# Check if we already have a relay hook installed
existing = settings["hooks"]["UserPromptSubmit"]
already_installed = any("check-relay.sh" in (h.get("command", "") if isinstance(h, dict) else "") for h in existing)

if already_installed:
    # Update the existing entry
    settings["hooks"]["UserPromptSubmit"] = [
        hook_entry if (isinstance(h, dict) and "check-relay.sh" in h.get("command", "")) else h
        for h in existing
    ]
    print("Updated existing relay hook in settings.json")
else:
    settings["hooks"]["UserPromptSubmit"].append(hook_entry)
    print("Added relay hook to settings.json")

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PYEOF

echo ""
echo "Done! The relay notification hook is installed."
echo "Claude Code will now check for unread relay messages at the start of each prompt."
echo ""
echo "To uninstall, remove the UserPromptSubmit entry from ${SETTINGS_FILE}"
echo "and delete ${CLAUDE_DIR}/check-relay.sh and ${ENV_FILE}."
