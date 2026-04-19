#!/usr/bin/env python3
"""
End-to-end test suite for the agentic-chat relay server.

Expects the relay to be running on localhost:4444.
Creates fresh tokens for testing and exercises all major functionality.
"""

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

BASE_URL = "http://localhost:4444"
DB_PATH = "./data/relay.db"

# Track results
results = []


def report(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    results.append((name, passed, detail))
    icon = "\033[32mPASS\033[0m" if passed else "\033[31mFAIL\033[0m"
    print(f"  [{icon}] {name}" + (f" -- {detail}" if detail else ""))


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ── Helper: create a token directly in the DB ──────────────────

def create_token(owner_name: str, namespace: str = "test_ns") -> str:
    """Create a token directly in the DB and return the raw token string."""
    raw_token = f"relay_tok_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now_ms = int(time.time() * 1000)

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO tokens (token_hash, owner_name, namespace, created_at) "
        "VALUES (?, ?, ?, ?)",
        (token_hash, owner_name, namespace, now_ms),
    )
    conn.commit()
    conn.close()
    return raw_token


def cleanup_tokens(namespace: str):
    """Remove test tokens from the DB."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM tokens WHERE namespace = ?", (namespace,))
    conn.execute("DELETE FROM peers WHERE namespace = ?", (namespace,))
    conn.execute("DELETE FROM messages WHERE namespace = ?", (namespace,))
    conn.execute(
        "DELETE FROM channels WHERE namespace = ?", (namespace,)
    )
    conn.execute("DELETE FROM cursors WHERE namespace = ?", (namespace,))
    conn.commit()
    conn.close()


# ── MCP protocol helpers ──────────────────────────────────────

MCP_HEADERS_BASE = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def parse_sse_json(text: str) -> dict | None:
    """Parse SSE response body to extract JSON data from 'event: message' blocks."""
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                continue
    # Maybe it's plain JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


async def mcp_initialize(client: httpx.AsyncClient, token: str) -> str | None:
    """Send MCP initialize and return session ID."""
    resp = await client.post(
        f"{BASE_URL}/mcp",
        headers={**MCP_HEADERS_BASE, "Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        },
    )
    if resp.status_code != 200:
        return None
    session_id = resp.headers.get("mcp-session-id")
    return session_id


async def mcp_initialized_notification(client: httpx.AsyncClient, token: str, session_id: str):
    """Send the initialized notification."""
    await client.post(
        f"{BASE_URL}/mcp",
        headers={
            **MCP_HEADERS_BASE,
            "Authorization": f"Bearer {token}",
            "Mcp-Session-Id": session_id,
        },
        json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        },
    )


async def mcp_call_tool(
    client: httpx.AsyncClient,
    token: str,
    session_id: str,
    tool_name: str,
    arguments: dict,
    req_id: int = 2,
) -> dict:
    """Call an MCP tool and return the parsed result."""
    resp = await client.post(
        f"{BASE_URL}/mcp",
        headers={
            **MCP_HEADERS_BASE,
            "Authorization": f"Bearer {token}",
            "Mcp-Session-Id": session_id,
        },
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        },
    )
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}

    data = parse_sse_json(resp.text)
    if data is None:
        return {"error": f"Could not parse response: {resp.text[:300]}"}

    if "error" in data:
        return {"error": data["error"]}

    # Extract the tool result from the MCP response
    content = data.get("result", {}).get("content", [])
    if content and isinstance(content, list) and content[0].get("text"):
        return json.loads(content[0]["text"])
    return data


class MCPPeer:
    """Convenience wrapper for an MCP peer session."""

    def __init__(self, client: httpx.AsyncClient, name: str, token: str):
        self.client = client
        self.name = name
        self.token = token
        self.session_id: str | None = None
        self._req_id = 10

    async def connect(self) -> bool:
        self.session_id = await mcp_initialize(self.client, self.token)
        if self.session_id:
            await mcp_initialized_notification(self.client, self.token, self.session_id)
            return True
        return False

    async def call(self, tool: str, args: dict) -> dict:
        self._req_id += 1
        return await mcp_call_tool(
            self.client, self.token, self.session_id, tool, args, self._req_id
        )


# ══════════════════════════════════════════════════════════════
#  TEST 1: CLI Commands
# ══════════════════════════════════════════════════════════════

def test_cli():
    section("1. CLI Commands")

    # 1a. init (in a temp dir)
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        proc = subprocess.run(
            [sys.executable, "relay.py", "init"],
            input="4445\ntest_cli_ns\n",
            capture_output=True,
            text=True,
            cwd=tmpdir,
            env=env,
            timeout=10,
        )
        # relay.py is in /data/mert/agentic-chat, but we need to run from tmpdir
        # Actually, relay.py uses relative paths for config. Let's copy it.

    # Run init properly by copying the project structure
    with tempfile.TemporaryDirectory() as tmpdir:
        # Copy relay.py and src to tmpdir
        shutil.copy2("relay.py", tmpdir)
        shutil.copytree("src", os.path.join(tmpdir, "src"))

        proc = subprocess.run(
            [sys.executable, "relay.py", "init"],
            input="4445\ntest_cli_ns\n",
            capture_output=True,
            text=True,
            cwd=tmpdir,
            timeout=10,
        )
        init_ok = proc.returncode == 0 and "relay_tok_" in proc.stdout
        report("init", init_ok, proc.stderr.strip()[:100] if not init_ok else "")

        # 1b. token create (in the tmpdir where init created the DB)
        proc = subprocess.run(
            [sys.executable, "relay.py", "token", "create", "--name", "testpeer",
             "--namespace", "test_cli_ns"],
            capture_output=True,
            text=True,
            cwd=tmpdir,
            timeout=10,
        )
        token_create_ok = proc.returncode == 0 and "relay_tok_" in proc.stdout
        report("token create", token_create_ok, proc.stderr.strip()[:100] if not token_create_ok else "")

        # 1c. token list
        proc = subprocess.run(
            [sys.executable, "relay.py", "token", "list"],
            capture_output=True,
            text=True,
            cwd=tmpdir,
            timeout=10,
        )
        token_list_ok = proc.returncode == 0 and "testpeer" in proc.stdout
        report("token list", token_list_ok, proc.stdout[:200] if not token_list_ok else "")

    # 1d. check --url against the live server (run from the real project dir)
    proc = subprocess.run(
        [sys.executable, "relay.py", "check", "--url", BASE_URL],
        capture_output=True,
        text=True,
        cwd="/data/mert/agentic-chat",
        timeout=10,
    )
    check_ok = proc.returncode == 0 and "[OK]" in proc.stdout and "Server responding" in proc.stdout
    report("check --url", check_ok, proc.stdout.strip()[-200:] if not check_ok else "")


# ══════════════════════════════════════════════════════════════
#  TEST 2: Full MCP Protocol Flow
# ══════════════════════════════════════════════════════════════

async def test_mcp_flow():
    section("2. Full MCP Protocol Flow")

    ns = f"mcp_test_{secrets.token_hex(4)}"

    # Create tokens
    alice_tok = create_token("alice", ns)
    bob_tok = create_token("bob", ns)
    carol_tok = create_token("carol", ns)
    outsider_tok = create_token("outsider", f"{ns}_other")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            alice = MCPPeer(client, "alice", alice_tok)
            bob = MCPPeer(client, "bob", bob_tok)
            carol = MCPPeer(client, "carol", carol_tok)
            outsider = MCPPeer(client, "outsider", outsider_tok)

            # Connect all peers
            a_ok = await alice.connect()
            b_ok = await bob.connect()
            c_ok = await carol.connect()
            o_ok = await outsider.connect()
            report("MCP init (alice, bob, carol, outsider)", all([a_ok, b_ok, c_ok, o_ok]))

            # 2a. All 3 heartbeat with status messages
            ha = await alice.call("heartbeat", {"status_message": "deploying v2.1"})
            hb = await bob.call("heartbeat", {"status_message": "reviewing PRs"})
            hc = await carol.call("heartbeat", {"status_message": "writing tests"})
            hb_ok = all(h.get("ok") for h in [ha, hb, hc])
            report("heartbeat (alice, bob, carol)", hb_ok,
                   f"alice={ha.get('ok')}, bob={hb.get('ok')}, carol={hc.get('ok')}")

            # 2b. alice broadcasts to #general
            send_gen = await alice.call("send", {
                "channel": "general",
                "content": "Deploying v2.1 to production now. ETA 5 minutes."
            })
            report("alice broadcast to #general", send_gen.get("ok", False))

            # 2c. bob and carol receive the broadcast
            bob_recv = await bob.call("receive", {"channel": "general"})
            carol_recv = await carol.call("receive", {"channel": "general"})
            bob_saw = any("Deploying v2.1" in m.get("content", "") for m in bob_recv.get("messages", []))
            carol_saw = any("Deploying v2.1" in m.get("content", "") for m in carol_recv.get("messages", []))
            report("bob receives broadcast", bob_saw,
                   f"got {bob_recv.get('count', 0)} msgs")
            report("carol receives broadcast", carol_saw,
                   f"got {carol_recv.get('count', 0)} msgs")

            # 2d. bob asks a question in #general
            bob_q = await bob.call("send", {
                "channel": "general",
                "content": "Any DB migrations in this release?"
            })
            report("bob sends question in #general", bob_q.get("ok", False))

            # 2e. alice DMs carol privately
            alice_dm = await alice.call("send", {
                "channel": "dm-alice-carol",
                "content": "Hey Carol, can you check the auth module after deploy? Found a potential issue."
            })
            report("alice DMs carol", alice_dm.get("ok", False),
                   f"channel={alice_dm.get('channel')}")

            # 2f. carol receives both the general msg and the DM
            carol_all = await carol.call("receive", {})
            carol_msgs = carol_all.get("messages", [])
            has_general = any("DB migrations" in m.get("content", "") for m in carol_msgs)
            has_dm = any("auth module" in m.get("content", "") for m in carol_msgs)
            report("carol receives general + DM", has_general and has_dm,
                   f"got {len(carol_msgs)} msgs, general={has_general}, dm={has_dm}")

            # 2g. carol replies to alice's DM
            carol_reply = await carol.call("send", {
                "channel": "dm-alice-carol",
                "content": "Sure, I'll check it right after the deploy finishes."
            })
            report("carol replies to DM", carol_reply.get("ok", False))

            # 2h. alice receives carol's DM reply
            alice_recv = await alice.call("receive", {"channel": "dm-alice-carol"})
            alice_msgs = alice_recv.get("messages", [])
            alice_got_reply = any("check it right after" in m.get("content", "") for m in alice_msgs)
            report("alice receives DM reply", alice_got_reply,
                   f"got {len(alice_msgs)} msgs")

            # 2i. list_peers shows all 3 online
            peers_resp = await alice.call("list_peers", {})
            peer_names = [p["name"] for p in peers_resp.get("peers", [])]
            all_visible = "bob" in peer_names and "carol" in peer_names
            report("list_peers shows bob+carol", all_visible,
                   f"peers={peer_names}")

            # 2j. list_channels shows general + dm-alice-carol
            channels_resp = await alice.call("list_channels", {})
            ch_names = [c["name"] for c in channels_resp.get("channels", [])]
            has_gen_ch = "general" in ch_names
            has_dm_ch = "dm-alice-carol" in ch_names
            report("list_channels shows general + DM", has_gen_ch and has_dm_ch,
                   f"channels={ch_names}")

            # 2k. Namespace isolation: outsider can't see any of the above
            outsider_hb = await outsider.call("heartbeat", {})
            outsider_peers = await outsider.call("list_peers", {})
            outsider_channels = await outsider.call("list_channels", {})
            outsider_recv = await outsider.call("receive", {})

            # outsider should see no peers from test ns, no channels, no messages
            o_peer_names = [p["name"] for p in outsider_peers.get("peers", [])]
            o_ch_names = [c["name"] for c in outsider_channels.get("channels", [])]
            o_msgs = outsider_recv.get("messages", [])
            isolation_ok = (
                "alice" not in o_peer_names
                and "bob" not in o_peer_names
                and "carol" not in o_peer_names
                and "general" not in o_ch_names
                and len(o_msgs) == 0
            )
            report("namespace isolation (outsider sees nothing)", isolation_ok,
                   f"outsider peers={o_peer_names}, channels={o_ch_names}, msgs={len(o_msgs)}")

    finally:
        cleanup_tokens(ns)
        cleanup_tokens(f"{ns}_other")


# ══════════════════════════════════════════════════════════════
#  TEST 3: Dashboard API
# ══════════════════════════════════════════════════════════════

async def test_dashboard():
    section("3. Dashboard API")

    ns = f"dash_test_{secrets.token_hex(4)}"
    dash_tok = create_token("dashuser", ns)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # 3a. GET /dashboard returns 200 with HTML
            resp = await client.get(f"{BASE_URL}/dashboard")
            report("GET /dashboard returns 200", resp.status_code == 200,
                   f"status={resp.status_code}")

            # 3b. GET /dashboard/api without auth returns 401
            resp = await client.get(f"{BASE_URL}/dashboard/api")
            report("GET /dashboard/api without auth = 401", resp.status_code == 401,
                   f"status={resp.status_code}")

            # 3c. GET /dashboard/api with valid token returns scoped data
            headers = {"Authorization": f"Bearer {dash_tok}"}
            resp = await client.get(f"{BASE_URL}/dashboard/api", headers=headers)
            ok_3c = resp.status_code == 200 and resp.json().get("you") == "dashuser"
            report("GET /dashboard/api with token = 200 + scoped data", ok_3c,
                   f"status={resp.status_code}, you={resp.json().get('you') if resp.status_code==200 else 'N/A'}")

            # 3d. POST /dashboard/api/send with valid token
            resp = await client.post(
                f"{BASE_URL}/dashboard/api/send",
                headers={**headers, "Content-Type": "application/json"},
                json={"channel": "general", "content": "Hello from dashboard test!"},
            )
            ok_3d = resp.status_code == 200 and resp.json().get("ok")
            report("POST /dashboard/api/send", ok_3d,
                   f"status={resp.status_code}")

            # 3e. Verify message visible via MCP receive
            # First init MCP session
            peer = MCPPeer(client, "dashuser", dash_tok)
            await peer.connect()
            recv = await peer.call("receive", {"channel": "general"})
            msgs = recv.get("messages", [])
            found = any("Hello from dashboard test!" in m.get("content", "") for m in msgs)
            report("dashboard message visible via MCP receive", found,
                   f"got {len(msgs)} msgs")

    finally:
        cleanup_tokens(ns)


# ══════════════════════════════════════════════════════════════
#  TEST 4: Join Page
# ══════════════════════════════════════════════════════════════

async def test_join_page():
    section("4. Join Page")

    ns = f"join_test_{secrets.token_hex(4)}"
    join_tok = create_token("joinuser", ns)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # 4a. GET /join/<valid_token> returns 200 with peer name in HTML
            resp = await client.get(f"{BASE_URL}/join/{join_tok}")
            ok_4a = resp.status_code == 200 and "joinuser" in resp.text
            report("GET /join/<valid_token> = 200 + peer name", ok_4a,
                   f"status={resp.status_code}")

            # 4b. GET /join/relay_tok_invalid returns 404
            resp = await client.get(f"{BASE_URL}/join/relay_tok_invalidtoken123")
            report("GET /join/relay_tok_invalid = 404", resp.status_code == 404,
                   f"status={resp.status_code}")

            # 4c. GET /join/badprefix returns 400
            resp = await client.get(f"{BASE_URL}/join/badprefix")
            report("GET /join/badprefix = 400", resp.status_code == 400,
                   f"status={resp.status_code}")

    finally:
        cleanup_tokens(ns)


# ══════════════════════════════════════════════════════════════
#  TEST 5: agent.py
# ══════════════════════════════════════════════════════════════

async def test_agent():
    section("5. agent.py")

    # 5a. --help works
    proc = subprocess.run(
        [sys.executable, "agent.py", "--help"],
        capture_output=True,
        text=True,
        cwd="/data/mert/agentic-chat",
        timeout=10,
    )
    report("agent.py --help", proc.returncode == 0 and "agent" in proc.stdout.lower(),
           f"rc={proc.returncode}")

    # 5b. --quiet connects and discovers identity, then exits
    ns = f"agent_test_{secrets.token_hex(4)}"
    agent_tok = create_token("agentbot", ns)

    try:
        proc = subprocess.Popen(
            [sys.executable, "agent.py",
             "--token", agent_tok,
             "--url", BASE_URL,
             "--quiet"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd="/data/mert/agentic-chat",
        )

        # The --quiet flag means it connects, discovers identity, then returns
        # (since it skips the loop). Give it time to finish.
        try:
            stdout, stderr = proc.communicate(timeout=15)
            # With --quiet, it should connect then exit cleanly
            connected = "Connected as" in stderr or "agentbot" in stderr
            report("agent.py --quiet connects and discovers identity", connected,
                   f"rc={proc.returncode}, stderr={stderr[:200]}")
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            connected = "Connected as" in stderr or "agentbot" in stderr
            report("agent.py --quiet connects and discovers identity", connected,
                   f"timed out but stderr={stderr[:200]}")
    finally:
        cleanup_tokens(ns)


# ══════════════════════════════════════════════════════════════
#  TEST 6: Rate Limiter
# ══════════════════════════════════════════════════════════════

async def test_rate_limiter():
    section("6. Rate Limiter")

    ns = f"rate_test_{secrets.token_hex(4)}"
    rate_tok = create_token("ratelimited", ns)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            headers = {
                **MCP_HEADERS_BASE,
                "Authorization": f"Bearer {rate_tok}",
            }

            # First, initialize an MCP session (costs 1 request from the burst)
            sess_id = await mcp_initialize(client, rate_tok)
            if sess_id:
                await mcp_initialized_notification(client, rate_tok, sess_id)

            # Wait a moment for bucket to refill a bit
            await asyncio.sleep(1)

            # Fire 30 rapid requests (within burst window)
            # The burst capacity is 30, and we've already used ~2-3 for init
            # so let's be precise: fire requests and count successes
            success_count = 0
            first_429_at = None

            # We need to send enough to exhaust the bucket.
            # After init (2-3 requests) + 1 second refill (5 tokens),
            # we should have ~30 tokens. Let's fire 35 to be sure we hit 429.
            req_headers = {**headers, "Mcp-Session-Id": sess_id} if sess_id else headers
            for i in range(35):
                resp = await client.post(
                    f"{BASE_URL}/mcp",
                    headers=req_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 100 + i,
                        "method": "tools/call",
                        "params": {
                            "name": "heartbeat",
                            "arguments": {},
                        },
                    },
                )
                if resp.status_code == 200:
                    success_count += 1
                elif resp.status_code == 429:
                    if first_429_at is None:
                        first_429_at = i
                    break

            report("burst requests succeed (>=25 of 35)", success_count >= 25,
                   f"succeeded={success_count}, first_429_at={first_429_at}")

            # Fire one more -- should get 429
            resp = await client.post(
                f"{BASE_URL}/mcp",
                headers=req_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 200,
                    "method": "tools/call",
                    "params": {"name": "heartbeat", "arguments": {}},
                },
            )
            report("request after burst = 429", resp.status_code == 429,
                   f"status={resp.status_code}")

            # Wait 1 second for refill (5 tokens/sec)
            await asyncio.sleep(1.5)

            resp = await client.post(
                f"{BASE_URL}/mcp",
                headers=req_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 300,
                    "method": "tools/call",
                    "params": {"name": "heartbeat", "arguments": {}},
                },
            )
            report("request after 1.5s wait succeeds", resp.status_code == 200,
                   f"status={resp.status_code}")

    finally:
        cleanup_tokens(ns)


# ══════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════

def main():
    print("\n" + "=" * 60)
    print("  AGENTIC-CHAT RELAY END-TO-END TEST SUITE")
    print("=" * 60)
    print(f"  Target: {BASE_URL}")
    print(f"  DB:     {DB_PATH}")

    # Verify server is up
    try:
        resp = httpx.get(f"{BASE_URL}/health", timeout=5)
        if resp.status_code != 200:
            print(f"\n  ERROR: Server returned {resp.status_code} on /health")
            sys.exit(1)
        print(f"  Health: {resp.json()}")
    except Exception as e:
        print(f"\n  ERROR: Cannot reach server at {BASE_URL}: {e}")
        sys.exit(1)

    # Run tests
    test_cli()
    asyncio.run(test_mcp_flow())
    asyncio.run(test_dashboard())
    asyncio.run(test_join_page())
    asyncio.run(test_agent())
    asyncio.run(test_rate_limiter())

    # Summary
    section("SUMMARY")
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)

    for name, ok, detail in results:
        if not ok:
            print(f"  \033[31mFAIL\033[0m {name}: {detail}")

    print(f"\n  {passed}/{total} passed, {failed} failed")
    if failed == 0:
        print("  \033[32mALL TESTS PASSED\033[0m")
    else:
        print(f"  \033[31m{failed} TEST(S) FAILED\033[0m")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
