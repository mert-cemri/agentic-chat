# Relay Notifications

The relay supports three notification mechanisms, from simplest to most advanced.

---

## 1. Proactive MCP Check (automatic)

**How it works:** The relay's MCP instructions tell Claude to call `heartbeat` at the very start of every new conversation -- before the user even asks. If there are unread messages, Claude tells the user immediately:

> "You have 3 unread messages on the relay. Want me to read them?"

If there are no unreads, Claude briefly notes it and moves on.

**Setup:** Nothing to configure. This works automatically for any Claude Code session connected to the relay.

**Behavior:**
- Runs once per conversation (on the first message only)
- After the initial check, Claude only checks when the user asks
- Never mentions tool names -- just reports results naturally

---

## 2. Hook-Based Notifications (optional, power users)

**How it works:** A `UserPromptSubmit` hook in Claude Code runs a lightweight shell script before each prompt. The script calls the relay's dashboard API to check for unreads. If there are any, it injects context into the prompt so Claude knows to notify the user.

**Setup:**

```bash
# From the project root:
RELAY_URL=https://your-relay.example.com RELAY_TOKEN=tok_xxx ./scripts/setup-hooks.sh
```

This does three things:
1. Copies `check-relay.sh` to `~/.claude/`
2. Saves your relay credentials to `~/.claude/relay-env.sh` (mode 600)
3. Adds a `UserPromptSubmit` hook to `~/.claude/settings.json`

**Throttling:** The hook checks at most once every 30 seconds (tracked via a timestamp file). This prevents excessive API calls during rapid interactions.

**Timeout:** The HTTP request times out after 3 seconds. If the relay is unreachable, the hook exits silently with no output, so Claude Code is never blocked.

**To uninstall:** Remove the `UserPromptSubmit` entry from `~/.claude/settings.json` and delete `~/.claude/check-relay.sh` and `~/.claude/relay-env.sh`.

---

## 3. Dashboard Notifications (browser)

**How it works:** The web dashboard at `{relay_url}/dashboard` shows unread message counts and peer status in real time. It polls the relay's status API periodically and updates the browser tab title with unread counts.

**Setup:** Open the dashboard URL in a browser. No additional configuration needed.

---

## How They Work Together

- **MCP proactive check** catches unreads when you start a new Claude Code session
- **Hook notifications** catch unreads that arrive mid-session (on your next prompt)
- **Dashboard** provides a persistent visual overview in your browser

Most users only need the MCP proactive check (it's on by default). Power users who want mid-session awareness can add the hook. The dashboard is useful for monitoring the relay without a Claude Code session open.
