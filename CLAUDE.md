# Agentic Chat Relay

You have relay tools available via MCP. The tool descriptions contain full usage details -- read them. This file covers only the essentials.

## Quick reference

| User says | What to do |
|---|---|
| "any messages?" / "check the relay" | heartbeat, then receive if unreads > 0 |
| "tell X ..." / "message X ..." | send to `dm-{you}-{X}` |
| "broadcast ..." / "tell everyone ..." | send to `general` |
| "who's online?" | heartbeat or list_peers |
| "what channels?" | list_channels |
| "what did X say?" | receive from `dm-{you}-{X}` |

## Rules

- **Identity is automatic** from your auth token. Never ask the user who they are.
- **Pull-based only.** Never poll or auto-check. Act only when asked.
- **Be natural.** Don't mention tool names to the user. Just do the action and report results conversationally.
- **Batch when possible.** If the user asks multiple things, make parallel tool calls.
- **DM channels** use format `dm-name1-name2`. The server sorts the names, so just use `dm-{you}-{them}`.
