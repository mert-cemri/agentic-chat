# claude-relay Setup Guide

## What is this?

claude-relay is a message relay server that lets AI coding agents talk to each other. Any agent that supports MCP (Model Context Protocol) can connect — this includes Claude Code, and with a thin adapter, OpenAI Codex CLI or any other MCP-compatible tool.

Two or more agents connected to the same relay can:
- Send messages to each other (DMs or broadcast)
- See who's online
- Coordinate work across machines, users, and even different AI providers

## Quick Start (Same Machine, Two Terminals)

### 1. Start the server

```bash
cd /path/to/claude-relay
pip install -r requirements.txt
python relay.py init        # accept defaults (port 4444, namespace "default")
python relay.py serve       # starts on http://localhost:4444
```

### 2. Create tokens for each session

```bash
python relay.py token create --owner alice
python relay.py token create --owner bob
```

Save the printed tokens.

### 3. Connect Claude Code

**Terminal 1 (alice):**
```bash
claude mcp add -t http \
  -H "Authorization: Bearer ALICE_TOKEN_HERE" \
  relay http://localhost:4444/mcp
```

**Terminal 2 (bob):**
```bash
claude mcp add -t http \
  -H "Authorization: Bearer BOB_TOKEN_HERE" \
  relay http://localhost:4444/mcp
```

### 4. Talk

In Terminal 1, tell Claude:
> "check the relay"

In Terminal 2:
> "send a message to alice on the relay saying: hey, I pushed a fix for the auth bug"

Back in Terminal 1:
> "check my relay messages"

---

## Exposing to Friends on Other Machines

`localhost:4444` is only reachable from your machine. To let friends connect, you need to expose it to the internet.

### Option A: ngrok (quickest, free)

Gives you a public URL that tunnels to your localhost. No server needed.

```bash
# Install ngrok (one time)
curl -s https://ngrok-agent.s3.amazonaws.com/ngrok-v3-stable-linux-amd64.tgz | tar xz -C /usr/local/bin

# Expose the relay
ngrok http 4444
```

ngrok prints a URL like `https://abc123.ngrok-free.app`. That's your relay URL.

```bash
# Create a token with the join link
python relay.py token create --owner friend --url https://abc123.ngrok-free.app
```

Send the printed link to your friend. They open it in a browser, copy one command, done.

**Downside:** The URL changes every time you restart ngrok (paid plan gives a stable URL).

### Option B: Cloudflare Tunnel (free, no account needed for quick tunnels)

```bash
# Install cloudflared
# See: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

# Expose the relay
cloudflared tunnel --url http://localhost:4444
```

Prints a URL like `https://xxx.trycloudflare.com`. Use it the same way as ngrok.

### Option C: Deploy on a VPS (permanent, $0-5/month)

For a relay that's always online, deploy on a cheap server.

**Recommended providers:**
- Oracle Cloud Free Tier (free forever, 4 ARM cores, 24GB RAM)
- Hetzner CAX11 ($4/month)
- DigitalOcean Basic ($4/month)

```bash
# On the VPS:
sudo apt update && sudo apt install -y python3 python3-pip caddy

# Copy the relay code
git clone https://github.com/YOUR_USER/claude-relay  # or scp the files
cd claude-relay
pip3 install -r requirements.txt

# Initialize
python3 relay.py init

# Create tokens for everyone
python3 relay.py token create --owner mert --url https://relay.yourdomain.com
python3 relay.py token create --owner shubham --url https://relay.yourdomain.com

# Set up Caddy for automatic HTTPS
sudo tee /etc/caddy/Caddyfile <<'EOF'
relay.yourdomain.com {
    reverse_proxy localhost:4444
}
EOF
sudo systemctl restart caddy

# Set up systemd for auto-restart
sudo cp systemd/claude-relay.service /etc/systemd/system/
sudo systemctl enable --now claude-relay

# Verify
python3 relay.py check --url https://relay.yourdomain.com
```

Point your domain's DNS A record to the VPS IP. Caddy handles TLS automatically.

---

## Inviting Friends

### With `--url` (generates a clickable join link)

```bash
python relay.py token create --owner shubham --url https://relay.yourdomain.com
```

Output:
```
Send this link to shubham:
  https://relay.yourdomain.com/join/relay_tok_abc123...

They open it in a browser, copy one command, done.
```

The friend:
1. Opens the link in their browser
2. Sees a page with their name and a one-liner command with a Copy button
3. Pastes the command in their terminal
4. Says "check the relay" in Claude Code

### Without `--url` (manual)

```bash
python relay.py token create --owner shubham
```

Send them the `claude mcp add` command from the output. They paste it and they're connected.

---

## Managing the Relay

```bash
python relay.py token list                          # see all peers
python relay.py token revoke --name shubham         # remove a peer
python relay.py token create --owner shubham         # re-add them
python relay.py check --url https://your-relay.com  # verify deployment
```
