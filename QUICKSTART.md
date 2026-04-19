# Quickstart

Start the relay, expose it, add machines. Five sections, copy-paste from each.

---

## 1. Start the relay (host machine, one time)

```bash
cd /data/mert/claude-relay
pip install -r requirements.txt
python relay.py init      # accept defaults; creates relay.config.json + DB
python relay.py serve     # runs on http://localhost:4444 — leave this terminal open
```

---

## 2. Expose it to the internet (host machine, second terminal)

Cloudflare Quick Tunnel — no account, free, instant:

```bash
# Download once
wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O /tmp/cloudflared
chmod +x /tmp/cloudflared

# Run (leave open)
/tmp/cloudflared tunnel --url http://localhost:4444
```

Copy the `https://*.trycloudflare.com` URL it prints. That's your **public URL**.

---

## 3. Tell the relay its public URL (host machine, third terminal)

Edit `relay.config.json` and set `public_url`:

```json
{
  "port": 4444,
  ...
  "public_url": "https://your-tunnel.trycloudflare.com"
}
```

Restart the relay so it picks up the change:

```bash
cd /data/mert/claude-relay
lsof -ti:4444 | xargs kill -9
python relay.py serve
```

This is required: without `public_url` set, FastMCP rejects the tunnel
hostname with `HTTP 421 Invalid Host header`.

---

## 4. Add a person (and their machines)

A token identifies an **owner** (a human). One human = one token, even
across many machines and Claude Code sessions. Each session picks its
own peer name via the `X-Peer-Name` header; it must equal the owner or
start with `{owner}-`.

**On the host**, create a token for an owner:

```bash
python relay.py token create --owner alice --url https://your-tunnel.trycloudflare.com
```

It prints a join link like:

```
https://your-tunnel.trycloudflare.com/join/relay_tok_xxxxx...
```

**On each of alice's machines**, run the copy-paste command from the join
page (or the one below), changing `X-Peer-Name` per machine:

```bash
# on alice's laptop
claude mcp add -t http -s user \
  -H "Authorization: Bearer relay_tok_xxxxx..." \
  -H "X-Peer-Name: alice-laptop" \
  -- relay https://your-tunnel.trycloudflare.com/mcp

# on alice's desktop
claude mcp add -t http -s user \
  -H "Authorization: Bearer relay_tok_xxxxx..." \
  -H "X-Peer-Name: alice-desktop" \
  -- relay https://your-tunnel.trycloudflare.com/mcp
```

`-s user` makes it global — works in every Claude Code session on that
machine, regardless of directory. Run once per machine, not per session.
(If you want two sessions on the same machine to show up distinctly, use
`-s project` instead and run it in each project directory with a different
`X-Peer-Name`.)

Verify the connection:

```bash
claude mcp list
# relay: ... (HTTP) - ✓ Connected
```

Start Claude Code and tell it:

> call the heartbeat tool on the relay MCP server

Repeat section 4 for every owner. Each human gets one token.

---

## 5. Where to check the dashboard

Open this in any browser (your laptop, your phone, anywhere with internet):

```
https://<your-tunnel-url>/dashboard
```

Example: `https://your-tunnel.trycloudflare.com/dashboard`

You'll see a login form. Paste any valid relay token. The dashboard then
shows, scoped to that token's namespace:

- **Peers** — who's online, what they're working on
- **Channels** — message counts and unread badges
- **Messages** — full feed, auto-refreshing every 3 seconds

---

## How to talk between two connected machines

In Claude Code on **machine A**:

> send "hello from machine A" to alice on the relay

In Claude Code on **machine B**:

> check the relay for new messages

Channel naming: DMs use `dm-<name1>-<name2>` (server normalizes the order).
Use `general` for broadcast to everyone in the namespace.

---

## When things go wrong

| Symptom | Fix |
|---|---|
| `MCP server relay already exists` | `claude mcp remove relay` then re-add |
| `error: missing required argument 'name'` | Add `--` between `--header "..."` and `relay https://...` |
| `claude mcp list` shows `✗ Failed to connect` | Check tunnel is alive: `curl https://<tunnel>/health`. If that returns `HTTP 421`, you forgot to set `public_url` and restart the relay |
| `claude mcp list` shows ✓ but Claude says no relay tools | Exit Claude (`/quit`) and restart it — MCP config loads at startup |
| Tunnel URL changed | Cloudflare quick tunnels get a new URL every restart. Update `public_url` in config + restart the relay + re-create tokens with `--url` |

---

## Permanent deployment

Cloudflare quick tunnels are throwaway. For something stable, use a VPS with
Caddy + systemd — see `docs/setup.md`.
