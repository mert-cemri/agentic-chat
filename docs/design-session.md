# Design Session: Agentic Chat v1

## Round 1
**PM:** The name "agentic-chat" is wrong. The README title says "claude-relay," the repo is "agentic-chat," the dashboard title says "Agentic Chat," and the product strategy says to position it as "coordination infrastructure." We need ONE name, used everywhere. I propose we go with **claude-relay** as the package name and public identity, and "relay" as the short form. "Agentic chat" sounds like a chatbot wrapper; "relay" sounds like infrastructure.

**UX:** Agree. Users will type `claude-relay` in pip, say "the relay" in conversation, and see "Claude Relay" in the dashboard. One word, one concept.
**Engineer A:** The PyPI name `claude-relay` is likely available. The internal package can stay `agentic_chat` or rename to `claude_relay` — I'd rename for consistency.
**Engineer B:** The CLAUDE.md already uses "relay" consistently. Good sign.
**Stylist:** The dashboard title should be "Claude Relay" not "Agentic Chat." The favicon SVG is fine, keep it.

**Decision:** Rename to **claude-relay** everywhere: repo, PyPI package, dashboard title, README header. Internal Python package becomes `claude_relay`. "Relay" is the short form in docs and conversation.

---

## Round 2
**UX:** The first-time experience is brutal. 12 steps across 3 terminals to get two agents talking. The "magic moment" — seeing a message arrive in another terminal — takes 5-10 minutes. We need a `claude-relay demo` command that collapses this to ONE command. It should: start the server, create two tokens, create two project directories with pre-configured `.mcp.json`, open the dashboard in a browser, and print "now run `cd ~/relay-demo-a && claude` in one terminal and `cd ~/relay-demo-b && claude` in another."

**PM:** This is the highest-priority item. If people can't get to the magic moment, nothing else matters.
**Engineer B:** I can build this. The `.mcp.json` file format is documented. The tricky part is that `claude mcp add` writes to `~/.claude.json` or `.mcp.json` depending on scope — we should write `.mcp.json` directly instead of shelling out to `claude mcp add`.
**Engineer A:** Agree. Write the file directly. Also, start the server in background mode and add a cleanup hint: "run `claude-relay demo --cleanup` to stop the server and remove demo directories."

**Decision:** Build `claude-relay demo` command. One command, auto-creates everything, prints two terminal commands to copy-paste, opens dashboard. Target: magic moment in under 90 seconds.

---

## Round 3
**Stylist:** The README opens with "A message relay server that lets multiple Claude Code sessions talk to each other." That's a description, not a hook. The first 10 seconds on the GitHub page determine whether someone reads further. I propose this structure: (1) one-line tagline, (2) 3-sentence scenario that makes you want it, (3) a GIF showing two terminals + dashboard, (4) `pip install claude-relay && claude-relay demo`, (5) everything else. The tagline should be: **"Let your AI agents talk to each other."**

**PM:** Love the tagline. But "AI agents" might be too broad — this is specifically for coding agents. What about "Let your Claude Code sessions talk to each other"?
**UX:** "AI agents" is better — it covers Cursor, Codex, Cline too. The compatibility table proves this isn't Claude-only.
**Engineer B:** The scenario in the product strategy doc is perfect: "You're debugging a distributed system. One Claude reads backend logs, another reads frontend errors. They correlate the root cause without you copy-pasting between terminals."

**Decision:** Restructure README: tagline "Let your AI agents talk to each other" at the top, followed by a concrete debugging scenario (3 sentences), then a placeholder for a GIF, then `pip install claude-relay && claude-relay demo`. Move the wall of setup instructions below the fold.

---

## Round 4
**Engineer A:** The `public_url` configuration is a landmine. Users have to manually edit `relay.config.json`, set `public_url`, then restart the server. If they forget, they get `HTTP 421 Invalid Host header` with no explanation. Two fixes: (1) accept `--public-url` as a CLI flag on `serve` so you don't have to edit a JSON file, and (2) print a clear error message when a request arrives with a Host header that doesn't match — something like "Request rejected: Host 'abc.trycloudflare.com' not in allowed hosts. Run `claude-relay serve --public-url https://abc.trycloudflare.com` to fix."

**UX:** The error message fix is critical. "421 Invalid Host" means nothing to a normal developer. The helpful error is the difference between a 2-minute fix and a 30-minute debugging session.
**PM:** Both changes, ship them together. The flag is easy; the error message saves support burden.
**Engineer B:** Also add auto-detection: if a request comes through with an X-Forwarded-Host that we haven't seen, log a warning with the exact command to run. Proactive, not reactive.

**Decision:** Add `--public-url` flag to `serve` command. Replace the opaque 421 error with a human-readable message that includes the fix command. Log a warning with the correct `--public-url` command when an unknown host is detected.

---

## Round 5
**Engineer B:** The dashboard login is a bare input field asking you to "paste your token." Most users won't have a token handy — they created it 10 minutes ago in a terminal that scrolled away. Two improvements: (1) the login page should explain what a token is and where to find it ("run `claude-relay token list` in your terminal"), and (2) add a "remember me" checkbox that stores the token in localStorage so you don't have to paste it every time you open the dashboard.

**Stylist:** The login card itself looks fine visually. But yes, the empty input with no help text is cold. Add a subtitle: "Paste your relay token to view your namespace." And a small "Where do I find my token?" expandable hint.
**UX:** "Remember me" is table stakes. Every dashboard does this. It's jarring that it doesn't persist.
**Engineer A:** Security note: localStorage tokens are fine for this use case. These aren't bank credentials. Add a "Sign out" button that clears it (I see there's already a sign-out button in the CSS, so just make sure it clears localStorage).

**Decision:** Add help text to dashboard login explaining where to find your token. Add "remember me" checkbox using localStorage. Ensure sign-out clears the stored token.

---

## Round 6
**PM:** The autonomous agent (`agent.py`) is buried at the bottom of the README and requires a separate Python invocation with manual token management. But it's the most compelling feature — it's what makes this "agentic" and not just "chat." Proposal: promote agents to a first-class CLI subcommand: `claude-relay agent --name worker1 --cwd /path/to/project`. If the relay is local, it auto-creates the token. If remote, you pass `--url` and `--token`. This also means fixing the `--quiet` bug (lines 396-406 where `--quiet` skips the entire agent loop).

**Engineer A:** The `--quiet` bug is embarrassing — it's essentially `--do-nothing`. Fix is straightforward: the `else` branch on the quiet check accidentally wraps the main loop.
**Engineer B:** For the CLI subcommand, I'd have `claude-relay agent start --name worker1 --cwd .` and `claude-relay agent stop --name worker1`. Maybe `claude-relay agent list` to see running agents. Process management is hard though — do we daemonize?
**PM:** Don't daemonize in v1. Just run in foreground. Document `nohup` or `&` for backgrounding. Keep it simple.

**Decision:** Fix the `--quiet` bug. Add `claude-relay agent` subcommand that wraps agent.py with sensible defaults. Auto-create token when relay is local. No daemonization in v1 — foreground process, document backgrounding.

---

## Round 7
**UX:** The first message in a new Claude Code session is awkward. You have to say "call the heartbeat tool on the relay MCP server" — that's not how humans talk to an AI assistant. The CLAUDE.md helps, but only if it's in the working directory. For users who `claude mcp add` from any directory, there's no context. Proposal: enrich the MCP tool descriptions so Claude understands the relay without CLAUDE.md. The `heartbeat` tool description should say: "Check in with the relay. Returns your identity, who else is online, and any unread message counts. Call this first when the user mentions the relay, messages, or other people." This makes "any new messages?" work out of the box.

**Engineer A:** MCP tool descriptions are set in the server code. We already have descriptions, but they're terse. Making them richer costs nothing — the descriptions are sent once during tool listing and aren't on the hot path.
**Engineer B:** Also, MCP supports server-level instructions (the `instructions` field in the server metadata). We should set this to something like: "You are connected to a message relay. Other AI agents and humans can send you messages. When the user asks about messages, people, or the relay, use the relay tools. Call heartbeat first to discover your identity and see who's online."
**Stylist:** This is invisible to the user but dramatically improves the feel. It's the difference between "call the heartbeat tool" and just "any messages?"

**Decision:** Rewrite all MCP tool descriptions to be richer and more instructive. Add MCP server-level instructions that prime Claude to understand the relay concept. Goal: "any messages?" should work without CLAUDE.md present.

---

## Round 8
**Stylist:** The dashboard looks like a developer debug tool, not a product. The dark theme is fine, but it lacks personality. Three specific changes: (1) Add a proper empty state — when there are no messages, show "No messages yet. Connect a Claude Code session to get started." with the `claude mcp add` command, not just a blank panel. (2) The peer list should show what each peer is working on (their status text from heartbeat) more prominently — it's the most interesting data. (3) Add a subtle animation when new messages arrive — a quick fade-in or slide-in. The current "just appears" feels like a page reload, not a live feed.

**UX:** The empty state is important. A blank dashboard after login makes people think something is broken.
**Engineer B:** The fade-in animation is easy CSS — `@keyframes fadeIn` on new `.msg` elements. The empty state is just a conditional render. Both are low-effort, high-impact.
**PM:** Do (1) and (3). Skip (2) for now — the status text from heartbeat isn't reliable enough to display prominently.

**Decision:** Add informative empty state to dashboard with setup instructions. Add fade-in animation for new messages. Defer peer status prominence to later.

---

## Round 9
**Engineer A:** The relay has no built-in tunnel support. Getting from localhost to a public URL is 3-4 steps (install cloudflared, run it, edit config, restart). This is the #1 reason cross-machine use doesn't happen. Proposal: `claude-relay serve --tunnel` auto-downloads cloudflared if not present, starts a tunnel, sets the public URL, and prints the join link URL. One flag to go from local to public.

**UX:** This is the "invite a friend" enabler. Without it, the relay is a single-machine toy. With it, it becomes a collaboration tool.
**PM:** I like this but it's a lot of engineering. Can we start simpler? Detect if `cloudflared` is installed, and if so, offer to start the tunnel automatically. If not, print "install cloudflared to enable --tunnel" with the install command.
**Engineer B:** We could also support ngrok: `--tunnel cloudflare` or `--tunnel ngrok`. But that's scope creep. Start with cloudflared only — it's free and doesn't require an account.
**Engineer A:** Agreed. I'll detect the binary, start it, parse the URL from stderr, and set it. Fallback: clear error with install instructions.

**Decision:** Add `--tunnel` flag to `serve` that auto-starts cloudflared and configures the public URL. Detect if cloudflared is installed; if not, print install instructions. Cloudflared only in v1 — no ngrok support yet.

---

## Round 10
**Engineer B:** The `claude mcp add` command that users paste is long and error-prone: `claude mcp add --transport http --scope project --header "Authorization: Bearer relay_tok_..." -- relay http://localhost:4444/mcp`. The `--` separator is confusing, the `--scope project` is a workaround, and the `--header` flag with quotes often gets mangled by copy-paste. Can we generate a one-liner script instead? Something like: `curl -s https://relay-url/setup/TOKEN | bash` that runs the `claude mcp add` command with proper escaping?

**PM:** Absolutely not. `curl | bash` is a security anti-pattern and will get us roasted on Hacker News. The join page with a Copy button is the right approach — it's what we have, and it works.
**UX:** Agree, keep the join page. But improve it: make the Copy button more prominent (it's tiny and gray right now), add a success toast when copied, and add a "Verify" section that tells you to run `claude mcp list` to confirm the connection.
**Stylist:** The join page is too minimal. Add a brief "What is Claude Relay?" section with 2-3 bullet points so the person receiving the invite understands what they're joining, not just how to connect.
**Engineer A:** Also, the join page should detect if the token has already been used (via a heartbeat check) and warn: "This token is already active as peer X. If this isn't you, contact the relay operator."

**Decision:** Keep the join page approach (no curl|bash). Improve the join page: bigger Copy button with success feedback, "What is Claude Relay?" explainer section, verification instructions. Defer token-already-used detection to later.

---

## Round 11
**PM:** We need to cut scope. The product strategy lists 8 things that are "missing for real adoption." We can't do all of them. For v1, I want to focus on: (1) zero-friction setup (demo command, PyPI, tunnel), (2) polished first experience (README, dashboard, join page), and (3) fixing bugs (the --quiet agent bug). Everything else — push notifications, message threading, token expiry, VS Code extension — is v2. Agreed?

**UX:** Agreed. Threading and push notifications are important but they're features for retained users, not new users. We don't have retained users yet.
**Engineer A:** Agreed, but I'd add one more to v1: the `--public-url` flag and better error messages. It's small engineering, huge impact on the setup flow.
**Engineer B:** And the MCP tool description improvements — that's also small and directly affects first-use experience.
**Stylist:** Agreed. Focus on the first 5 minutes, not the 5th day.

**Decision:** v1 scope is: demo command, PyPI package, tunnel flag, README rewrite, dashboard polish, join page improvements, MCP tool descriptions, --public-url flag, --quiet bug fix. Everything else is v2.

---

## Round 12
**UX:** The README has a "Common gotchas" table at the bottom, but these gotchas hit users in the first 5 minutes — they should see them BEFORE they hit the errors. Proposal: instead of a troubleshooting section, bake the fixes into the instructions themselves. For example, instead of saying "run `claude mcp add ... -- relay ...`" and then listing "missing required argument 'name'" in gotchas, the instruction should say: "Run this command (the `--` is required — it separates the flags from the server name):" with a footnote. Inline the fixes, don't make people scroll to a troubleshooting table.

**Engineer B:** This is a documentation change but it's a good one. The `--` gotcha bites literally everyone.
**Stylist:** Keep the gotchas table too — it's useful for quick scanning when something goes wrong. But yes, the inline explanations should prevent most trips to the table.
**PM:** Do both. Inline the most common gotchas (the `--` separator, the `--scope project` requirement for same-machine testing) and keep a shorter troubleshooting table for edge cases.

**Decision:** Inline explanations for the top 3 gotchas (the `--` separator, `--scope project`, and "call heartbeat first") directly into the setup instructions. Keep a shorter troubleshooting table for less common issues.

---

## Round 13
**Stylist:** The CLI output is functional but not polished. When you run `python relay.py token create --name alice`, it dumps the token and a warning. Compare this to `gh auth login` or `npx create-next-app` — they use colors, spacing, and visual hierarchy. Specific proposals: (1) use bold/color for the token value so it stands out, (2) add a box around the join link (it's the thing people need to copy), (3) after `relay serve`, print a clean startup banner showing the URL, dashboard URL, and peer count, not just "serving on 0.0.0.0:4444."

**Engineer B:** Python's `rich` library would handle this, but that's a new dependency. We can use ANSI escape codes directly for bold, dim, and color — it's a few helper functions.
**PM:** Keep it lightweight. ANSI codes, no new dependencies. The startup banner is the most impactful — users stare at it while the server runs.
**Engineer A:** The startup banner should show: the server URL, the dashboard URL, the number of registered peers, and how to create a new token. Four lines, clear and useful.

**Decision:** Polish CLI output with ANSI formatting: bold token values, boxed join links, and a clean startup banner showing server URL, dashboard URL, peer count, and help hint. Use raw ANSI codes, no new dependencies.

---

## Round 14
**Engineer A:** The database is SQLite, which is perfect for single-machine use. But if someone deploys to a VPS and gets more than a few peers, they'll hit SQLite's write lock on concurrent heartbeats. I'm not proposing a Postgres migration — that's overkill. Instead: (1) use WAL mode (write-ahead logging) for SQLite, which allows concurrent reads during writes, and (2) batch heartbeat updates so we're not hitting the DB on every 3-second poll from every peer. These two changes scale SQLite to ~50 concurrent peers comfortably.

**PM:** Is anyone hitting this limit today?
**Engineer A:** Not yet, but if we're promoting this as team infrastructure, 10-20 peers is realistic. WAL mode is a one-line change. Batching is maybe 20 lines. Low effort, prevents a class of "it's slow/broken" reports.
**Engineer B:** WAL mode is definitely worth it — it's the standard for any SQLite server. Do it.

**Decision:** Enable SQLite WAL mode. Defer heartbeat batching unless performance issues are observed. WAL mode is a one-line change with significant concurrency benefit.

---

## Round 15
**Engineer B:** The `pyproject.toml` exists but there's no proper entry point or package structure ready for PyPI. To make `pip install claude-relay` work, we need: (1) a `[project.scripts]` section mapping `claude-relay` to the CLI entry point, (2) ensure `relay.py`, `agent.py`, and `static/` are included in the package, (3) a version number (start at `0.1.0`), and (4) a proper package description for PyPI. I can have this ready in a few hours.

**PM:** This is the second highest priority after the demo command. `pip install` is how Python developers expect to install tools. `git clone` is how they expect to inspect source code. We're a tool, not a library.
**Stylist:** The PyPI page is a discovery channel. Write a good `long_description` — it should be the README. Make sure the metadata includes keywords: "mcp", "claude", "agent", "relay", "multi-agent."
**Engineer A:** Pin the dependency versions in `pyproject.toml`. We don't want `pip install claude-relay` to break because `mcp` shipped a breaking change.

**Decision:** Finalize `pyproject.toml` for PyPI publishing: entry point `claude-relay` -> `cli.main()`, include static assets, version `0.1.0`, pin dependency versions, use README as long description. This is the blocker for all distribution.

---

## Round 16
**PM:** The "send a message from the dashboard" feature is mentioned in the product strategy as missing, but I see there's actually a compose bar in the dashboard CSS. Is it implemented?

**Engineer B:** Yes, the compose bar exists in the HTML and CSS. It has a channel selector and a send button. It works via a POST to the API using the dashboard token. This was added recently.
**PM:** Good. Then the dashboard is further along than the product strategy suggests. Let's make sure it works well: the channel selector should default to the currently selected channel in the sidebar, and the compose input should auto-focus when you select a channel. These are small UX wins.
**UX:** Also, add keyboard shortcut: Enter to send, Shift+Enter for newline. That's the universal chat convention.
**Stylist:** The compose bar blends into the dark background. Add a subtle top border or slight background differentiation so it's visually distinct from the message area.

**Decision:** Polish the dashboard compose bar: default to selected channel, auto-focus input on channel switch, Enter to send / Shift+Enter for newline. The compose bar already exists, so this is polish, not new feature work.

---

## Round 17
**UX:** The peer names in this system are set at token creation time and can't be changed. If someone creates a token with `--name bob` and Bob's actual name is Robert, he's "bob" forever. This is fine for now, but the bigger issue is that peer names must be unique within a namespace and there's no validation feedback if you pick a name that's taken. Proposal: when `token create --name X` is called and X already exists, print a clear error: "Peer 'X' already exists in namespace 'default'. Use a different name or revoke the existing token with `claude-relay token revoke --name X`."

**Engineer A:** This is probably already handled at the DB level with a unique constraint, but the error message is likely a raw SQL exception. Wrapping it in a user-friendly message is the right call.
**PM:** Agreed, this is a 15-minute fix with outsized impact on the setup experience.

**Decision:** Add user-friendly error message when creating a token with a name that already exists. Include the revoke command in the error message.

---

## Round 18
**Stylist:** The join page (`/join/<token>`) is too minimal. It has a command to paste and a security note, but the person receiving this link has no idea what Claude Relay is. They got a URL from a friend with no context. The join page should have: (1) a one-line description: "Claude Relay lets AI coding agents talk to each other across machines," (2) the peer name they'll be joining as, prominently displayed, (3) a "What you'll need" checklist (Claude Code installed, internet access), (4) the command to paste with a large Copy button, and (5) a "What happens next" section explaining the first steps after connecting.

**UX:** The "What you'll need" checklist is important — if they don't have Claude Code installed, the rest is useless. Link to the Claude Code install page.
**Engineer B:** The join page is a single static HTML template with string substitution. Adding content is trivial. I'd also add the relay operator's name (the namespace owner) so the invitee knows who invited them.
**PM:** Don't over-design it. The join page should be scannable in 10 seconds. Description, prerequisites, command, done.

**Decision:** Expand the join page with: a one-line product description, a "What you'll need" prereq checklist (with Claude Code install link), and a "What happens next" section. Keep it scannable — no essays.

---

## Round 19
**Engineer A:** The health check endpoint (`/health`) returns basic status, but there's no readiness check that verifies the database is accessible and the MCP layer is functional. For production deployments behind load balancers or in containers, a real health check should verify: DB is readable, at least one namespace exists, and the server can accept MCP connections. Proposal: add `/health/ready` that does a lightweight DB query and returns 200 or 503.

**PM:** Is this v1 scope? We said focus on first 5 minutes, not production operations.
**Engineer A:** It's 10 lines of code and it prevents a class of "the relay is running but broken" silent failures. The Dockerfile already exists — anyone using Docker will want this.
**Engineer B:** Agree, it's trivial. Also useful for the `--tunnel` feature — after starting cloudflared, we should hit `/health/ready` to confirm the tunnel is working end-to-end before printing "your relay is live."

**Decision:** Add `/health/ready` endpoint that verifies DB connectivity. Use it internally to validate tunnel setup. Low effort, prevents silent failures.

---

## Round 20
**PM:** Let me close with prioritization. We made 19 decisions. Here's the implementation order based on impact and dependencies: (1) Fix the --quiet bug — it's a bug, just fix it. (2) PyPI packaging — everything else depends on `pip install` working. (3) Demo command — the #1 first-impression improvement. (4) README rewrite — the #1 discovery improvement. (5) MCP tool descriptions — invisible but critical for first-use. (6) --public-url flag + error messages — unblocks cross-machine use. (7) --tunnel flag — makes cross-machine use easy. (8) Dashboard polish (empty state, animation, compose) — makes the product feel real. (9) Join page improvements — helps the invite flow. (10) CLI output polish — makes every interaction feel professional. The rename to claude-relay happens alongside PyPI packaging.

**UX:** Agreed on the order. Items 1-5 should ship together as a single "v0.1.0" release. Items 6-10 can follow in a week.
**Engineer A:** I'd move WAL mode into the PyPI packaging work — it's one line in the DB init and should be in from the start.
**Engineer B:** Agreed. Let's also add the SQLite WAL mode and the /health/ready endpoint to the packaging work — both are trivial.
**Stylist:** The README rewrite should happen before PyPI publishing since the README becomes the PyPI long_description.

**Decision:** Implementation order established. Ship items 1-5 as v0.1.0. Items 6-10 follow as v0.1.1. README rewrite must happen before PyPI publish. WAL mode and /health/ready are bundled with packaging.

---

## Summary: Top 10 Decisions

1. **Rename to claude-relay everywhere.** Repo, PyPI package (`claude-relay`), dashboard title ("Claude Relay"), internal Python package (`claude_relay`). Kill "agentic-chat" as a user-facing name. The tagline is "Let your AI agents talk to each other."

2. **Build `claude-relay demo` command.** One command that starts the server, creates two tokens, writes two `.mcp.json` files into `~/relay-demo-a/` and `~/relay-demo-b/`, opens the dashboard in the browser, and prints two commands to copy-paste into separate terminals. Target: magic moment in under 90 seconds.

3. **Rewrite the README for 10-second comprehension.** Structure: tagline, 3-sentence scenario (distributed debugging), GIF placeholder, `pip install claude-relay && claude-relay demo`, compatibility table, then everything else below the fold. Inline the top 3 gotchas into setup instructions instead of relegating them to a troubleshooting table.

4. **Publish to PyPI as `claude-relay` v0.1.0.** Finalize `pyproject.toml` with entry point `claude-relay` -> CLI, include static assets, pin dependencies, use README as long description, add keywords (mcp, claude, agent, relay, multi-agent). Bundle SQLite WAL mode and `/health/ready` endpoint into this release.

5. **Enrich MCP tool descriptions and add server-level instructions.** Rewrite all 5 tool descriptions to be self-explanatory (e.g., heartbeat says "Call this first when the user mentions the relay, messages, or other people"). Add MCP server instructions field so Claude understands the relay concept without CLAUDE.md. Goal: "any messages?" works without priming.

6. **Add `--public-url` flag and human-readable host errors.** `claude-relay serve --public-url https://xxx.trycloudflare.com` replaces manual JSON editing. Replace the opaque 421 error with: "Host 'xxx' not in allowed hosts. Run `claude-relay serve --public-url https://xxx` to fix." Log a warning when an unknown host is detected.

7. **Add `--tunnel` flag for automatic cloudflared integration.** `claude-relay serve --tunnel` detects or downloads cloudflared, starts a tunnel, auto-configures the public URL, and prints the join link URL. If cloudflared isn't installed, print the install command. Validate tunnel with `/health/ready` before reporting success.

8. **Fix the `--quiet` bug in agent.py and add `claude-relay agent` subcommand.** The `--quiet` flag currently skips the entire agent loop (lines 396-406) — fix the indentation. Add `claude-relay agent --name worker1 --cwd /path/to/project` that auto-creates a token when the relay is local, connects, and runs in foreground.

9. **Polish the dashboard.** Three changes: (a) informative empty state with setup instructions when no messages exist, (b) fade-in animation for new messages, (c) compose bar polish — default to selected channel, auto-focus input, Enter to send / Shift+Enter for newline.

10. **Improve the join page and CLI output.** Join page: add product description, "What you'll need" prereq checklist with Claude Code install link, and "What happens next" section. CLI output: ANSI-formatted bold token values, boxed join links, and a startup banner showing server URL, dashboard URL, peer count, and next-step hint.
