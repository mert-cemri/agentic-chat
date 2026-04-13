"""Full HTTP stack integration tests.

These hit the real FastMCP app + auth middleware + DB via httpx ASGI client.
They exercise the entire request pipeline: HTTP -> auth -> MCP protocol -> tools -> DB.
"""

import hashlib
import json

import pytest
from relay import now_ms


# ------------------------------------------------------------------
# Auth middleware (HTTP layer)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint_no_auth(http_client):
    """Health endpoint works without auth."""
    resp = await http_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["server"] == "claude-relay"
    assert "db_size_bytes" in data


@pytest.mark.asyncio
async def test_mcp_missing_auth_header(http_client):
    """MCP endpoint rejects missing Authorization header."""
    resp = await http_client.post(
        "/mcp",
        headers={"Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 401
    data = resp.json()
    assert "Missing" in data["error"]
    assert "hint" in data


@pytest.mark.asyncio
async def test_mcp_malformed_auth_header(http_client):
    """MCP endpoint rejects malformed Authorization header."""
    resp = await http_client.post(
        "/mcp",
        headers={"Content-Type": "application/json", "Authorization": "NotBearer xyz"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mcp_invalid_token(http_client):
    """MCP endpoint rejects unknown token with 403."""
    resp = await http_client.post(
        "/mcp",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer relay_tok_nonexistent_token",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 403
    assert "Invalid" in resp.json()["error"]


# ------------------------------------------------------------------
# MCP protocol handshake
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_initialize_handshake(seeded_db, mcp_client):
    """Initialize returns protocol version and server info."""
    _, tokens = seeded_db
    client = mcp_client(tokens["alice"], "alice")
    result = await client.initialize()
    assert "result" in result
    assert result["result"]["serverInfo"]["name"] == "claude-relay"
    assert "tools" in result["result"]["capabilities"]
    assert client.session_id  # got a session ID


@pytest.mark.asyncio
async def test_mcp_list_tools(seeded_db, mcp_client):
    """tools/list returns all 5 relay tools."""
    _, tokens = seeded_db
    client = mcp_client(tokens["alice"], "alice")
    await client.initialize()
    result = await client.list_tools()
    tool_names = {t["name"] for t in result["result"]["tools"]}
    assert tool_names == {"heartbeat", "send", "receive", "list_peers", "list_channels"}


# ------------------------------------------------------------------
# Full tool flow via HTTP
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_via_http(seeded_db, mcp_client):
    """Heartbeat tool call via full HTTP stack."""
    _, tokens = seeded_db
    client = mcp_client(tokens["alice"], "alice")
    await client.initialize()
    result = await client.call_tool("heartbeat", {"status_message": "testing"})
    assert result["ok"] is True
    assert result["you"]["peer_name"] == "alice"
    assert result["you"]["namespace"] == "default"


@pytest.mark.asyncio
async def test_send_receive_http_flow(seeded_db, mcp_client):
    """Full send/receive loop via HTTP between two peers."""
    _, tokens = seeded_db
    alice = mcp_client(tokens["alice"], "alice")
    bob = mcp_client(tokens["bob"], "bob")

    await alice.initialize()
    await bob.initialize()

    # Alice sends a DM
    send_result = await alice.call_tool("send", {
        "channel": "dm-alice-bob",
        "content": "Hello bob, testing over HTTP",
    })
    assert send_result["ok"] is True
    assert send_result["channel"] == "dm-alice-bob"
    msg_id = send_result["message_id"]

    # Bob receives
    recv_result = await bob.call_tool("receive", {"channel": "dm-alice-bob"})
    assert recv_result["ok"] is True
    assert len(recv_result["messages"]) == 1
    assert recv_result["messages"][0]["content"] == "Hello bob, testing over HTTP"
    assert recv_result["messages"][0]["id"] == msg_id
    assert recv_result["messages"][0]["from"] == "alice"

    # Bob receives again -> empty (cursor advanced)
    recv2 = await bob.call_tool("receive", {"channel": "dm-alice-bob"})
    assert recv2["ok"] is True
    assert len(recv2["messages"]) == 0


@pytest.mark.asyncio
async def test_dm_normalization_over_http(seeded_db, mcp_client):
    """DM names get normalized regardless of order."""
    _, tokens = seeded_db
    alice = mcp_client(tokens["alice"], "alice")
    bob = mcp_client(tokens["bob"], "bob")
    await alice.initialize()
    await bob.initialize()

    # Alice sends as dm-bob-alice (reverse order)
    r1 = await alice.call_tool("send", {
        "channel": "dm-bob-alice",
        "content": "msg 1",
    })
    assert r1["channel"] == "dm-alice-bob"  # normalized

    # Bob sends as dm-alice-bob (normal order)
    r2 = await bob.call_tool("send", {
        "channel": "dm-alice-bob",
        "content": "msg 2",
    })
    assert r2["channel"] == "dm-alice-bob"

    # Both messages should be in the same channel
    result = await alice.call_tool("list_channels", {})
    dm_channels = [c for c in result["channels"] if c["name"].startswith("dm-")]
    assert len(dm_channels) == 1
    assert dm_channels[0]["total_messages"] == 2


@pytest.mark.asyncio
async def test_all_channels_receive_over_http(seeded_db, mcp_client):
    """Receiving without a channel arg returns unread from all channels."""
    _, tokens = seeded_db
    alice = mcp_client(tokens["alice"], "alice")
    bob = mcp_client(tokens["bob"], "bob")
    await alice.initialize()
    await bob.initialize()

    await alice.call_tool("send", {"channel": "general", "content": "g1"})
    await alice.call_tool("send", {"channel": "random", "content": "r1"})
    await alice.call_tool("send", {"channel": "dm-alice-bob", "content": "d1"})

    result = await bob.call_tool("receive", {})
    assert result["ok"] is True
    assert len(result["messages"]) == 3
    # Should have channel info per message in all-channels mode
    channels_seen = {m["channel"] for m in result["messages"]}
    assert channels_seen == {"general", "random", "dm-alice-bob"}


@pytest.mark.asyncio
async def test_namespace_isolation_over_http(test_db, mcp_client):
    """Peers in different namespaces cannot see each other's data."""
    # Set up two peers in different namespaces
    raw_tokens = {}
    for name, ns in [("alice", "team-a"), ("mallory", "team-b")]:
        raw = f"relay_tok_{name}_{ns}"
        h = hashlib.sha256(raw.encode()).hexdigest()
        await test_db.execute(
            "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
            "VALUES (?, ?, ?, ?)",
            (h, name, ns, now_ms()),
        )
        raw_tokens[name] = raw

    alice = mcp_client(raw_tokens["alice"], "alice")
    mallory = mcp_client(raw_tokens["mallory"], "mallory")
    await alice.initialize()
    await mallory.initialize()

    # Alice sends a secret in team-a
    await alice.call_tool("send", {"channel": "secret", "content": "team-a internal"})

    # Mallory in team-b tries to read it
    r = await mallory.call_tool("receive", {"channel": "secret"})
    assert r["ok"] is False  # Channel doesn't exist in her namespace
    assert "does not exist" in r["error"]

    # Mallory's all-channels receive should be empty
    r = await mallory.call_tool("receive", {})
    assert r["ok"] is True
    assert len(r["messages"]) == 0

    # Mallory's list_peers should not show alice
    r = await mallory.call_tool("list_peers", {})
    assert r["ok"] is True
    names = [p["name"] for p in r["peers"]]
    assert "alice" not in names


# ------------------------------------------------------------------
# Dashboard + join page over HTTP
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_html_loads(http_client):
    """Dashboard HTML is served without auth."""
    resp = await http_client.get("/dashboard")
    assert resp.status_code == 200
    assert "Agentic Chat" in resp.text
    assert "<!DOCTYPE html>" in resp.text


@pytest.mark.asyncio
async def test_dashboard_api_requires_auth(http_client):
    """Dashboard API rejects unauthenticated requests."""
    resp = await http_client.get("/dashboard/api")
    assert resp.status_code == 401
    data = resp.json()
    assert "Unauthorized" in data["error"] or "Authorization" in data.get("hint", "")


@pytest.mark.asyncio
async def test_dashboard_api_rejects_invalid_token(http_client):
    """Dashboard API rejects invalid bearer tokens."""
    resp = await http_client.get(
        "/dashboard/api", headers={"Authorization": "Bearer relay_tok_bogus"}
    )
    # Dashboard endpoint handles its own auth — both missing and invalid return 401
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_dashboard_api_scoped_to_namespace(seeded_db, http_client):
    """Dashboard API returns only the caller's namespace."""
    _, tokens = seeded_db
    resp = await http_client.get(
        "/dashboard/api",
        headers={"Authorization": f"Bearer {tokens['alice']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["namespace"] == "default"
    assert data["you"] == "alice"
    assert "peers" in data
    assert "channels" in data
    assert "messages" in data
    # All 3 seeded peers are in 'default' namespace
    assert len(data["peers"]) == 3
    for p in data["peers"]:
        assert p["namespace"] == "default"


@pytest.mark.asyncio
async def test_dashboard_api_does_not_leak_other_namespaces(test_db, mcp_client, http_client):
    """A peer's dashboard view must not contain messages from other namespaces."""
    # Set up alice in team-a and mallory in team-b
    raw_tokens = {}
    for name, ns in [("alice", "team-a"), ("mallory", "team-b")]:
        raw = f"relay_tok_dash_{name}_{ns}"
        h = hashlib.sha256(raw.encode()).hexdigest()
        await test_db.execute(
            "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
            "VALUES (?, ?, ?, ?)",
            (h, name, ns, now_ms()),
        )
        raw_tokens[name] = raw

    alice = mcp_client(raw_tokens["alice"], "alice")
    mallory = mcp_client(raw_tokens["mallory"], "mallory")
    await alice.initialize()
    await mallory.initialize()

    await alice.call_tool("send", {"channel": "secret", "content": "team-a confidential"})
    await mallory.call_tool("send", {"channel": "secret", "content": "team-b internal"})

    # Alice's dashboard view should only show team-a data
    resp = await http_client.get(
        "/dashboard/api",
        headers={"Authorization": f"Bearer {raw_tokens['alice']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["namespace"] == "team-a"
    contents = {m["content"] for m in data["messages"]}
    assert "team-a confidential" in contents
    assert "team-b internal" not in contents
    # No messages should have a non-alice namespace
    for m in data["messages"]:
        assert m["namespace"] == "team-a"


@pytest.mark.asyncio
async def test_join_page_with_valid_token(seeded_db, http_client):
    """Join page renders HTML with the peer's name and a copy-paste command."""
    _, tokens = seeded_db
    resp = await http_client.get(f"/join/{tokens['alice']}")
    assert resp.status_code == 200
    assert "alice" in resp.text
    assert "claude mcp add" in resp.text
    assert tokens["alice"] in resp.text


@pytest.mark.asyncio
async def test_join_page_with_invalid_token(http_client):
    """Join page returns 404 for nonexistent token."""
    resp = await http_client.get("/join/relay_tok_nonexistent")
    assert resp.status_code == 404
    assert "Invalid or expired" in resp.text


@pytest.mark.asyncio
async def test_join_page_with_bad_prefix(http_client):
    """Join page returns 400 for tokens without the proper prefix."""
    resp = await http_client.get("/join/notarelaytoken")
    assert resp.status_code == 400
    assert "Invalid token" in resp.text
