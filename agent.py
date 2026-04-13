#!/usr/bin/env python3
"""
agent.py — Autonomous Claude Code agent that monitors the relay and executes tasks.

This daemon polls the relay for new messages addressed to it (via DM or @mention),
executes the task using Claude Code (via the Agent SDK), and posts the result back.

Usage:
    python agent.py --token relay_tok_xxx --url http://localhost:4444 --cwd /path/to/project

The agent runs until killed. It:
1. Polls the relay every few seconds for new messages
2. When a message is addressed to it (DM or @agent_name), treats it as a task
3. Spawns a Claude Code subprocess to execute the task
4. Posts the result back to the same channel
5. Updates its status on the relay so others can see it's working

Requirements:
    pip install httpx claude-agent-sdk

The agent does NOT need the relay source code or Python relay server.
It only needs Claude Code CLI installed and a relay token.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx

# -- Logging -------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] agent: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("agent")


# -- Relay HTTP Client ---------------------------------------------


class RelayClient:
    """Thin HTTP client for the relay API (no MCP needed)."""

    def __init__(self, relay_url: str, token: str):
        self.base_url = relay_url.rstrip("/").replace("/mcp", "")
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}
        self._http: httpx.AsyncClient | None = None
        self.peer_name: str | None = None
        self.namespace: str | None = None
        # Track last seen message ID to only process new messages
        self._last_seen_id: int = 0

    async def connect(self) -> None:
        """Initialize the HTTP client and discover our identity via heartbeat MCP call."""
        self._http = httpx.AsyncClient(timeout=30)
        # Use the dashboard API to discover our identity
        resp = await self._http.get(
            f"{self.base_url}/dashboard/api",
            headers=self.headers,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to connect to relay: {resp.status_code} {resp.text}")
        data = resp.json()
        self.peer_name = data["you"]
        self.namespace = data["namespace"]
        log.info("Connected as '%s' in namespace '%s'", self.peer_name, self.namespace)

        # Set initial last_seen_id to current max message ID (don't process old messages)
        if data.get("messages"):
            self._last_seen_id = max(m["id"] for m in data["messages"])
            log.info("Starting from message ID %d (ignoring %d existing messages)",
                     self._last_seen_id, len(data["messages"]))

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()

    async def heartbeat(self, status: str) -> dict:
        """Update our status on the relay via dashboard API send."""
        # We use the MCP endpoint for heartbeat since dashboard API doesn't have one.
        # Instead, just set status via a special channel convention.
        # Actually, let's use the dashboard API to poll and send.
        # The heartbeat is implicit — polling the API updates last_used_at.
        return {}

    async def poll_new_messages(self) -> list[dict]:
        """Fetch messages newer than our last seen ID."""
        resp = await self._http.get(
            f"{self.base_url}/dashboard/api",
            headers=self.headers,
        )
        if resp.status_code != 200:
            log.warning("Poll failed: %d", resp.status_code)
            return []

        data = resp.json()
        messages = data.get("messages", [])

        # Filter to only messages after our last seen ID
        new_msgs = [m for m in messages if m["id"] > self._last_seen_id]

        if new_msgs:
            self._last_seen_id = max(m["id"] for m in new_msgs)

        return new_msgs

    async def send_message(self, channel: str, content: str) -> dict:
        """Send a message to a channel via the dashboard send API."""
        resp = await self._http.post(
            f"{self.base_url}/dashboard/api/send",
            headers={**self.headers, "Content-Type": "application/json"},
            json={"channel": channel, "content": content},
        )
        if resp.status_code != 200:
            log.warning("Send failed: %d %s", resp.status_code, resp.text[:200])
            return {"ok": False, "error": resp.text}
        return resp.json()

    def is_addressed_to_me(self, msg: dict) -> bool:
        """Check if a message is addressed to this agent.

        A message is "for us" if:
        1. It's in a DM channel that includes our name (dm-us-them)
        2. It starts with @our_name
        3. It's in a channel named 'tasks' or 'tasks-{our_name}'

        We ignore our own messages to prevent loops.
        """
        if msg["sender"] == self.peer_name:
            return False

        channel = msg.get("channel", "")

        # DM channel containing our name
        if channel.startswith("dm-") and self.peer_name in channel.split("-"):
            return True

        # @mention at the start
        content = msg.get("content", "")
        if content.lower().startswith(f"@{self.peer_name.lower()}"):
            return True

        # Tasks channel
        if channel in ("tasks", f"tasks-{self.peer_name}"):
            return True

        return False

    def extract_task(self, msg: dict) -> str:
        """Extract the task prompt from a message.

        Strips the @mention prefix if present.
        """
        content = msg.get("content", "")
        prefix = f"@{self.peer_name}"
        if content.lower().startswith(prefix.lower()):
            content = content[len(prefix):].strip()
            # Strip optional colon or comma after mention
            if content and content[0] in (":", ","):
                content = content[1:].strip()
        return content


# -- Task Execution ------------------------------------------------


async def execute_task(task_prompt: str, cwd: str, allowed_tools: list[str],
                       max_turns: int = 15, model: str | None = None) -> str:
    """Execute a task using the Claude Agent SDK.

    Spawns a Claude Code subprocess, gives it the task, and returns
    the final text output.
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage, TextBlock
    except ImportError:
        log.error("claude-agent-sdk not installed. Install with: pip install claude-agent-sdk")
        return "Error: claude-agent-sdk not installed on this machine."

    log.info("Executing task: %s", task_prompt[:100])

    options = ClaudeAgentOptions(
        allowed_tools=allowed_tools,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        cwd=cwd,
    )
    if model:
        options.model = model

    result_parts = []
    try:
        async for message in query(prompt=task_prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        result_parts.append(block.text.strip())
            elif isinstance(message, ResultMessage):
                if message.subtype == "success" and message.result:
                    result_parts.append(message.result)
                elif message.subtype != "success":
                    result_parts.append(f"[Task ended: {message.subtype}]")
    except Exception as e:
        log.exception("Task execution failed")
        result_parts.append(f"Error executing task: {e}")

    result = "\n\n".join(result_parts) if result_parts else "(no output)"

    # Truncate if too long for a relay message (keep under 45KB to leave room)
    if len(result) > 45000:
        result = result[:44900] + "\n\n... (output truncated, exceeded 45KB)"

    return result


# -- Main Loop -----------------------------------------------------


async def agent_loop(
    relay: RelayClient,
    cwd: str,
    poll_interval: float,
    allowed_tools: list[str],
    max_turns: int,
    model: str | None,
    watch_channels: list[str] | None,
) -> None:
    """Main agent loop: poll for messages, execute tasks, post results."""

    log.info(
        "Agent '%s' monitoring relay (poll every %.1fs, cwd: %s)",
        relay.peer_name,
        poll_interval,
        cwd,
    )
    log.info("Allowed tools: %s", ", ".join(allowed_tools))
    if watch_channels:
        log.info("Watching channels: %s", ", ".join(watch_channels))
    else:
        log.info("Watching: DMs + @mentions + tasks channel")

    consecutive_errors = 0
    while True:
        try:
            new_messages = await relay.poll_new_messages()
            consecutive_errors = 0

            for msg in new_messages:
                # Filter by watch_channels if specified
                if watch_channels and msg.get("channel") not in watch_channels:
                    if not relay.is_addressed_to_me(msg):
                        continue

                if not relay.is_addressed_to_me(msg):
                    continue

                task = relay.extract_task(msg)
                if not task:
                    continue

                channel = msg.get("channel", "general")
                sender = msg["sender"]

                log.info("Task from %s in #%s: %s", sender, channel, task[:100])

                # Acknowledge receipt
                await relay.send_message(
                    channel,
                    f"On it, @{sender}. Working on your request..."
                )

                # Execute the task
                result = await execute_task(
                    task_prompt=task,
                    cwd=cwd,
                    allowed_tools=allowed_tools,
                    max_turns=max_turns,
                    model=model,
                )

                # Post the result
                await relay.send_message(channel, result)
                log.info("Task completed, result posted to #%s (%d chars)", channel, len(result))

        except Exception as e:
            consecutive_errors += 1
            log.error("Poll error (%d consecutive): %s", consecutive_errors, e)
            if consecutive_errors > 10:
                log.error("Too many consecutive errors, backing off to 30s")
                await asyncio.sleep(30)
                continue

        await asyncio.sleep(poll_interval)


# -- CLI -----------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        prog="agent",
        description="Autonomous Claude Code agent that monitors the relay and executes tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Basic agent with default tools
  python agent.py --token relay_tok_xxx --url http://localhost:4444

  # Agent with specific working directory
  python agent.py --token relay_tok_xxx --url http://localhost:4444 --cwd /path/to/project

  # Agent that only watches a specific channel
  python agent.py --token relay_tok_xxx --url http://localhost:4444 --watch tasks

  # Agent with restricted tools (read-only, no code execution)
  python agent.py --token relay_tok_xxx --url http://localhost:4444 --tools Read,Glob,Grep

How tasks are triggered:
  - DM the agent: send a message to dm-you-agentname
  - @mention: @agentname please find all TODO comments
  - Tasks channel: send to #tasks or #tasks-agentname

The agent executes each task via Claude Code and posts the result back.
""",
    )

    parser.add_argument(
        "--token", required=True,
        help="Relay bearer token for this agent (relay_tok_...)",
    )
    parser.add_argument(
        "--url", required=True,
        help="Relay server URL (e.g. http://localhost:4444 or https://your-tunnel.trycloudflare.com)",
    )
    parser.add_argument(
        "--cwd", default=".",
        help="Working directory for task execution (default: current directory)",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=3.0,
        help="Seconds between relay polls (default: 3.0)",
    )
    parser.add_argument(
        "--tools", default="Read,Edit,Write,Bash,Glob,Grep",
        help="Comma-separated list of allowed Claude Code tools (default: Read,Edit,Write,Bash,Glob,Grep)",
    )
    parser.add_argument(
        "--max-turns", type=int, default=15,
        help="Max agentic turns per task (default: 15)",
    )
    parser.add_argument(
        "--model", default=None,
        help="Claude model to use (default: whatever Claude Code defaults to)",
    )
    parser.add_argument(
        "--watch", nargs="*", default=None,
        help="Only watch specific channels (in addition to DMs and @mentions)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Don't announce presence on startup",
    )

    args = parser.parse_args()

    cwd = str(Path(args.cwd).resolve())
    if not Path(cwd).is_dir():
        print(f"Error: working directory does not exist: {cwd}", file=sys.stderr)
        sys.exit(1)

    allowed_tools = [t.strip() for t in args.tools.split(",")]

    async def _run():
        relay = RelayClient(args.url, args.token)
        try:
            await relay.connect()

            # --quiet only suppresses the startup announcement, not the loop
            if not args.quiet:
                await relay.send_message(
                    "general",
                    f"Agent `{relay.peer_name}` is now online and monitoring for tasks. "
                    f"DM me or @{relay.peer_name} in any channel to assign work."
                )

            await agent_loop(
                relay=relay,
                cwd=cwd,
                poll_interval=args.poll_interval,
                allowed_tools=allowed_tools,
                max_turns=args.max_turns,
                model=args.model,
                watch_channels=args.watch,
            )
        except KeyboardInterrupt:
            log.info("Agent shutting down...")
            await relay.send_message(
                "general",
                f"Agent `{relay.peer_name}` is going offline.",
            )
        finally:
            await relay.close()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
