# Agentic Chat Relay

You have relay tools via MCP. Tool descriptions have full details — this is just the quick reference.

## Quick reference

| User says | What to do |
|---|---|
| "any messages?" / "check the relay" | heartbeat, then receive if unreads > 0 |
| "tell X ..." / "message X ..." | send to `dm-{you}-{X}` |
| "message alice and bob about Y" | send to `group-alice-bob-{you}` |
| "broadcast ..." / "tell everyone ..." | send to `general` |
| "note to self: ..." | send to `self-{you}` |
| "who's online?" | heartbeat or list_peers |
| "what channels?" | list_channels |

## Channel types

- **Broadcast:** `general`, `backend`, `frontend` — everyone sees it
- **DM:** `dm-alice-bob` — 1-on-1, server sorts names
- **Group:** `group-alice-bob-carol` — 3+ people, names sorted + deduped
- **Self:** `self-yourname` — personal notes, visible across all your sessions

## Rules

- **Identity is automatic.** Never ask the user who they are.
- **Proactive startup check.** Call heartbeat at the start of each new conversation. Report unreads.
- **Be natural.** Don't mention tool names. Just act and report conversationally.
- **Batch when possible.** Parallel tool calls for multiple questions.
