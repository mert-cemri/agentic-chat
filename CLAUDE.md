# Agentic Chat Relay

This project includes an MCP relay server. You have 5 relay tools available:
- `mcp__relay__heartbeat` — check in, get online peers and unread counts
- `mcp__relay__send` — send a message (params: `channel`, `text`)
- `mcp__relay__receive` — read messages (params: `channel` optional, `limit` optional)
- `mcp__relay__list_peers` — list all known peers and online status
- `mcp__relay__list_channels` — list channels you have access to

Your identity is determined by your auth token automatically. Do NOT poll or auto-check; only use the relay when the user asks.

## Natural language mapping

| User says | Action |
|---|---|
| "check the relay" / "any messages?" | `heartbeat`, then `receive` if unreads > 0 |
| "tell X ..." / "message X ..." / "send X ..." | `send` to channel `dm-{you}-{X}` with the message |
| "broadcast ..." / "tell everyone ..." | `send` to channel `general` |
| "who's online?" / "who's around?" | `list_peers` |
| "what channels?" | `list_channels` |
| "check messages from X" | `receive` from channel `dm-{you}-{X}` |
| "reply to X" / "respond to X" | `send` to `dm-{you}-{X}` |
| "read #channel-name" | `receive` from that channel |

## DM channel naming

DM channels use the format `dm-{name1}-{name2}` with names sorted alphabetically. The server normalizes this, so just use `dm-{yourname}-{theirname}` and it will resolve correctly.

## Examples

**User:** "any new messages?"
**Do:** Call `heartbeat`. If unreads exist, call `receive`. Summarize what you got.

**User:** "tell alice I pushed the fix to main"
**Do:** Call `send` with channel=`dm-{you}-alice`, text="I pushed the fix to main". Confirm it was sent.

**User:** "broadcast: standup in 5 minutes"
**Do:** Call `send` with channel=`general`, text="standup in 5 minutes". Confirm it was sent.

**User:** "who's online and do I have messages from bob?"
**Do:** Call `list_peers` and `receive` (channel=`dm-{you}-bob`) in parallel. Report both results.

## Key rules
- **Pull-based only.** Never poll or check automatically. Act only when the user asks.
- **Be natural.** Don't mention tool names to the user; just do the action and report results conversationally.
- **Batch when possible.** If the user asks multiple things, make parallel tool calls.
