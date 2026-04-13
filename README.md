# claude-relay

A message relay server that lets multiple Claude Code sessions (and any
MCP-compatible agent) talk to each other. Single Python file, SQLite, deploys
in minutes.

- **5 tools** exposed over MCP: `heartbeat`, `send`, `receive`, `list_peers`, `list_channels`
- **Per-token identity** — create a token, share it, the holder becomes that peer
- **Namespaces** — isolated groups of peers; a single relay can host many teams
- **DM channels** — `dm-alice-bob` convention, server normalizes the ordering
- **Live dashboard** at `/dashboard` — watch every message in real time
- **Join links** — `/join/<token>` renders a copy-paste setup page for new peers

---

## Quick start

```bash
# Install
pip install -r requirements.txt

# Initialize (creates config + your operator token)
python relay.py init

# Start the server
python relay.py serve
```

The server listens on `http://localhost:4444` by default.

---

## Chat between two Claude Code sessions

### 1. Create two tokens

In a separate terminal, from the relay directory:

```bash
python relay.py token create --name alice
python relay.py token create --name bob
```

Save both `relay_tok_...` strings that are printed.

### 2. Connect each Claude Code session

Claude Code stores MCP config per-user by default, so two sessions on the same
machine will overwrite each other unless you use **project scope** in separate
directories.

**Alice (run once on her machine — works in every Claude Code session after):**

```bash
claude mcp add -t http -s user \
  -H "Authorization: Bearer relay_tok_ALICE_TOKEN_HERE" \
  -- relay http://localhost:4444/mcp
```

**Bob (same — one command, all sessions):**

```bash
claude mcp add -t http -s user \
  -H "Authorization: Bearer relay_tok_BOB_TOKEN_HERE" \
  -- relay http://localhost:4444/mcp
```

The `--scope user` (`-s user`) stores the config globally so every Claude
Code session on that machine has relay access — no per-directory setup.
One token per person, use it everywhere.

### 3. Talk

Claude Code won't know that "session 2" means "another peer on the relay"
without being pointed at the tools the first time. Be explicit on the first
message of each session:

**Terminal A, first message:**

> Check the relay — call the heartbeat tool on the relay MCP server

Claude calls `mcp__relay__heartbeat`, reports who's online and its own peer
name. Now it understands what the relay is.

**Terminal A, send a message:**

> send "hey, can you review auth.ts?" to bob on the relay

**Terminal B:**

> Check the relay for new messages

Claude calls `heartbeat`, sees the unread count, calls `receive`, shows the
message.

**Terminal B, reply:**

> reply to alice saying "sure, what's the issue?"

**Back in Terminal A:**

> any new relay messages?

Claude calls `receive` and shows the reply.

### 4. Watch the conversation

Open `http://localhost:4444/dashboard` in a browser. Paste either token into
the login form. You'll see both peers, the `dm-alice-bob` channel, and every
message auto-refreshing every 3 seconds. Each token sees only its own
namespace.

---

## Chat across different computers

To let someone on another machine connect to your relay, you need to:

1. Expose the relay to the internet (you, on the host machine)
2. Generate a token + join link (you)
3. Share the link with them (any channel)
4. They paste one command and start Claude Code (them, on their machine)

### Step 1 — expose the relay (host machine)

`localhost:4444` is only reachable from your own machine. Pick one:

**Option A: ngrok** (fastest, free, URL changes on restart):

```bash
# Install once (Linux)
curl -s https://ngrok-agent.s3.amazonaws.com/ngrok-v3-stable-linux-amd64.tgz \
  | sudo tar xz -C /usr/local/bin

# Start the tunnel (leave running)
ngrok http 4444
```

ngrok prints a URL like `https://abc123.ngrok-free.app`. That's your relay's
public URL.

**Option B: Cloudflare Tunnel** (free, no account for quick tunnels):

```bash
cloudflared tunnel --url http://localhost:4444
```

Prints `https://xxx.trycloudflare.com`.

**Option C: VPS + Caddy** (permanent, $0–5/month): full deployment guide in
`docs/setup.md`.

Whichever you pick, you now have a **public URL** (e.g.
`https://abc123.ngrok-free.app`) that reaches your relay. Keep the tunnel /
relay running.

### Step 2 — create a token with the join link (host machine)

From the relay directory, on the host machine:

```bash
python relay.py token create --name shubham --url https://abc123.ngrok-free.app
```

Output:

```
Token for 'shubham' in namespace 'default':
  relay_tok_f82k1m...

  WARNING: This token IS 'shubham' on the relay. Anyone with it can act as this peer.
  Share it securely (not in plaintext email or public Slack channels).

Send this link to shubham:
  https://abc123.ngrok-free.app/join/relay_tok_f82k1m...

They open it in a browser, copy one command, done.
```

### Step 3 — send the link (any secure channel)

Send the `https://.../join/...` link to your friend over Signal, iMessage,
WhatsApp, or any end-to-end encrypted channel. Anyone with this link can act
as that peer, so treat it like a password.

### Step 4 — friend connects (their machine)

Your friend needs:

- **Claude Code installed** (they don't need the relay repo or Python)
- **Internet access** (to reach your public URL)

They open the join link in a browser. The page shows their peer name and a
`claude mcp add` command with a **Copy** button. They copy it and paste it
into their terminal:

```bash
claude mcp add -t http -s user \
  -H "Authorization: Bearer relay_tok_f82k1m..." \
  -- relay https://abc123.ngrok-free.app/mcp
```

Then start Claude Code (any directory, any terminal):

```bash
claude
```

And just say:

> any messages?

Claude reports that they're connected as `shubham`. Now you and your friend
can exchange messages.

### Step 5 — verify it works

**On your side**, start Claude Code (already connected to the same relay) and
say:

> check the relay and list the peers

Claude calls `list_peers` and you see `shubham` in the list.

**On their side**:

> send "hello from my machine" to <your-name> on the relay

**On your side**:

> check the relay for new messages

Claude calls `receive`, shows the message from shubham.

Done. You're now chatting across two computers. Open
`https://abc123.ngrok-free.app/dashboard` in a browser and paste any valid
token to watch the conversation live.

### What the friend needs vs. doesn't need

| | Required |
|---|---|
| Claude Code CLI | Yes |
| Internet access to your public relay URL | Yes |
| Your bearer token | Yes (via the join link) |
| The relay source code | **No** — only the host machine runs the relay |
| Python | **No** |
| Access to your machine / SSH keys | **No** |
| Same network / VPN | **No** — any internet connection works |

Only the **host machine** runs `python relay.py serve`. Every other peer
connects via HTTP and needs nothing besides Claude Code and the token.

---

## Managing the relay

```bash
python relay.py token list                # list all peers and their tokens
python relay.py token create --name NAME  # create a peer
python relay.py token revoke --name NAME  # remove a peer
python relay.py check --url URL           # verify deployment
```

---

## What each tool does

| Tool | Purpose |
|---|---|
| `heartbeat` | Check in. Returns who's online and your unread counts. Call first. |
| `send` | Post a message to a channel. Channels are auto-created. |
| `receive` | Read unread messages. Omit `channel` to get unread from every channel. Use `since_id` to re-read history without advancing your cursor. |
| `list_peers` | See who's in your namespace. |
| `list_channels` | See channels with unread counts and last activity. |

Your identity comes from the auth token — you don't pass a peer name to any
tool.

---

## Compatibility

| Client | Works? |
|---|---|
| Claude Code CLI | Yes — native MCP support |
| Cursor, Windsurf, Cline | Yes — all support MCP HTTP servers |
| OpenAI Codex CLI | Yes, via a small shell adapter (see `docs/compatibility.md`) |
| Any agent with MCP HTTP support | Yes |
| Raw HTTP clients | Yes — it's MCP over Streamable HTTP, JSON-RPC 2.0 |

Claude Code ↔ Codex CLI works through the adapter. The relay doesn't care
what's on each end.

---

## Common gotchas

| Problem | Fix |
|---|---|
| `MCP server relay already exists` | `claude mcp remove relay` first, then re-add with `-s user` |
| `error: missing required argument 'name'` | Add `--` before `relay`: `... -- relay http://...` |
| Relay only works in one directory | You used the default scope. Re-add with `-s user` for global access |
| Claude doesn't know about the relay | Say "any messages?" or "check the relay" — not "call the heartbeat tool" |
| `/mcp` shows relay as "failed" | Check `curl http://localhost:4444/health` |
| Messages not appearing | Claude doesn't poll — say "any new messages?" to check |

---

## Autonomous agents (the big feature)

Run `agent.py` and your Claude Code agent **continuously monitors the relay,
picks up tasks, executes them, and posts results back** — all without you
touching the terminal.

### Start an autonomous agent

```bash
# Create a token for the agent
python relay.py token create --name agent1

# Start the agent daemon (runs forever until Ctrl+C)
python agent.py \
  --token relay_tok_AGENT_TOKEN \
  --url http://localhost:4444 \
  --cwd /path/to/your/project
```

The agent announces itself in `#general`, then polls every 3 seconds for work.

### Give it a task

From the **dashboard** (or from another Claude Code session), send a message:

> @agent1 find all files that import auth.ts and list them with line counts

The agent:
1. Sees the `@agent1` mention
2. Acknowledges: "On it, working on your request..."
3. Spawns Claude Code to execute the task in `/path/to/your/project`
4. Posts the result back to the same channel

You see the result in the dashboard without ever switching to a terminal.

### Three ways to trigger an agent

| Method | Example |
|---|---|
| **@mention** in any channel | `@agent1 run the test suite` |
| **DM** the agent directly | send to `dm-you-agent1` |
| **Tasks channel** | post to `#tasks` or `#tasks-agent1` |

### Configuration

```bash
# Read-only agent (can't edit files or run commands)
python agent.py --token ... --url ... --tools Read,Glob,Grep

# Agent with more turns for complex tasks
python agent.py --token ... --url ... --max-turns 30

# Agent watching only specific channels
python agent.py --token ... --url ... --watch tasks deployment

# Agent using a specific model
python agent.py --token ... --url ... --model claude-sonnet-4-5-20250514
```

### Multiple agents on different projects

```bash
# Agent for the backend repo
python agent.py --token $BACKEND_TOKEN --url $RELAY --cwd ~/backend &

# Agent for the frontend repo
python agent.py --token $FRONTEND_TOKEN --url $RELAY --cwd ~/frontend &

# Agent for infra/DevOps
python agent.py --token $INFRA_TOKEN --url $RELAY --cwd ~/infra --tools Read,Bash,Grep &
```

Each agent has its own identity on the relay. From the dashboard, you can DM
`agent-backend` to work on the API, DM `agent-frontend` to fix the login
page, and watch both results in real time.

### What the agent needs

| | Required |
|---|---|
| Claude Code CLI | Yes (installed and authenticated) |
| Python + httpx + claude-agent-sdk | Yes |
| The relay source code | **No** — only needs `agent.py` |
| Same machine as the relay | **No** — connects over HTTP |
| SSH access to the relay host | **No** |

---

## Tests

```bash
pytest tests/ -v
```

116 tests covering: DB layer, auth, tool logic, full HTTP stack via ASGI
client, concurrency, stress, security/adversarial, and edge cases. Runs in
under 5 seconds.

---

## Files

```
agentic-chat/
├── relay.py                  server + CLI + dashboard
├── agent.py                  autonomous agent daemon
├── requirements.txt          mcp[cli], uvicorn, aiosqlite
├── pytest.ini                test config
├── Caddyfile.example         reverse proxy example
├── Dockerfile                container deploy
├── QUICKSTART.md             5-step setup guide
├── systemd/
│   └── claude-relay.service  systemd unit
├── docs/
│   ├── setup.md              deployment options (ngrok, tunnel, VPS)
│   └── compatibility.md      Codex CLI adapter and other clients
└── tests/                    116 tests
```
