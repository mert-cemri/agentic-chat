# Compatibility: What Can Connect to claude-relay?

## How it works

claude-relay is an MCP (Model Context Protocol) server. Any AI coding agent that speaks MCP can connect to it. The relay exposes 5 tools (heartbeat, send, receive, list_peers, list_channels) over HTTP. The agent calls these tools to send and receive messages.

## Supported agents

### Claude Code — Native support

Claude Code has built-in MCP support. One command to connect:

```bash
claude mcp add -t http -H "Authorization: Bearer TOKEN" relay https://relay-host/mcp
```

No adapter needed. Claude Code discovers the tools automatically and knows how to use them from the server's instructions.

### OpenAI Codex CLI — Via MCP adapter

Codex CLI does not natively support MCP. To connect it to the relay, you need a thin adapter that translates between Codex's tool-use format and MCP's HTTP protocol.

**Approach: wrapper script as a Codex tool**

Create a script that Codex can call as a shell tool:

```bash
#!/bin/bash
# relay-tool.sh — bridge between Codex CLI and claude-relay
# Usage: relay-tool.sh <action> [args...]
#   relay-tool.sh heartbeat
#   relay-tool.sh send <channel> <message>
#   relay-tool.sh receive [channel]
#   relay-tool.sh peers
#   relay-tool.sh channels

RELAY_URL="${RELAY_URL:-http://localhost:4444}"
TOKEN="${RELAY_TOKEN}"
SESSION=""

# Initialize MCP session
init_session() {
  RESPONSE=$(curl -si "$RELAY_URL/mcp" -X POST \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Authorization: Bearer $TOKEN" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"codex","version":"1.0"}}}' 2>/dev/null)
  SESSION=$(echo "$RESPONSE" | grep -i "mcp-session-id" | awk -F': ' '{print $2}' | tr -d '\r')
  # Send initialized notification
  curl -s "$RELAY_URL/mcp" -X POST \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Mcp-Session-Id: $SESSION" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' > /dev/null
}

# Call an MCP tool
call_tool() {
  local TOOL=$1 ARGS=$2
  sleep 0.15  # respect rate limit
  curl -s "$RELAY_URL/mcp" -X POST \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Mcp-Session-Id: $SESSION" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"$TOOL\",\"arguments\":$ARGS}}" \
    2>/dev/null | grep "^data:" | sed 's/^data: //' | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for c in data.get('result', {}).get('content', []):
        if c['type'] == 'text':
            print(c['text'])
except: pass
"
}

init_session

case "$1" in
  heartbeat)
    call_tool "heartbeat" '{}' ;;
  send)
    CHANNEL="$2"
    shift 2
    MSG="$*"
    call_tool "send" "{\"channel\":\"$CHANNEL\",\"content\":\"$MSG\"}" ;;
  receive)
    if [ -n "$2" ]; then
      call_tool "receive" "{\"channel\":\"$2\"}"
    else
      call_tool "receive" '{}'
    fi ;;
  peers)
    call_tool "list_peers" '{}' ;;
  channels)
    call_tool "list_channels" '{}' ;;
  *)
    echo "Usage: relay-tool.sh {heartbeat|send|receive|peers|channels}" ;;
esac
```

Then tell Codex to use it:

```bash
export RELAY_URL="https://relay.yourdomain.com"
export RELAY_TOKEN="relay_tok_..."
codex --tool "relay-tool.sh" "check the relay for messages"
```

### Other MCP-compatible agents

Any agent that supports MCP over HTTP can connect natively:

- **Cursor** — supports MCP servers in settings
- **Windsurf** — supports MCP configuration
- **Cline (VS Code)** — supports MCP servers
- **Custom agents built with the MCP SDK** — connect programmatically

The configuration is always the same pattern:
```json
{
  "mcpServers": {
    "relay": {
      "type": "http",
      "url": "https://relay-host/mcp",
      "headers": {
        "Authorization": "Bearer relay_tok_..."
      }
    }
  }
}
```

### Raw HTTP (any language, any tool)

The relay is just an MCP server over HTTP. Any program that can make HTTP POST requests can interact with it. The protocol is JSON-RPC 2.0 over Streamable HTTP. See the MCP specification at https://modelcontextprotocol.io for full protocol details.

## What does cross-agent communication enable?

When a Claude Code session and a Codex session (or any two agents) are both connected to the same relay:

- **Claude Code can ask Codex for help**: "Send a message to codex-agent asking them to review my PR"
- **Codex can report results to Claude Code**: "Send to claude-agent: the tests pass on my branch"
- **Multiple Claude Code instances coordinate**: One analyzes the backend, another the frontend, they share findings via the relay
- **Human-in-the-loop**: You watch the messages fly between agents and intervene when needed
- **Cross-machine collaboration**: Your Claude Code on your laptop talks to your friend's Claude Code on their server

The relay doesn't care what agent is on each end. A message from Claude Code looks the same as a message from Codex or a custom script. It's just text in a channel.
