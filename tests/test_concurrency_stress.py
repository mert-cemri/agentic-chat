"""Concurrency and stress tests.

Exercise the relay under concurrent load: many peers sending simultaneously,
race conditions on cursors, large message volumes, etc.
"""

import asyncio
import hashlib

import pytest
from relay import now_ms


async def seed_peer(db, name: str, ns: str = "default") -> str:
    raw = f"relay_tok_stress_{name}"
    h = hashlib.sha256(raw.encode()).hexdigest()
    await db.execute(
        "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
        "VALUES (?, ?, ?, ?)",
        (h, name, ns, now_ms()),
    )
    return raw


# ------------------------------------------------------------------
# Concurrency
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_10_peers_concurrent_heartbeat(test_db, mcp_client):
    """10 peers heartbeat simultaneously. All should succeed."""
    tokens = [await seed_peer(test_db, f"peer{i}") for i in range(10)]
    clients = [mcp_client(t, f"peer{i}") for i, t in enumerate(tokens)]

    # Init all sessions sequentially (MCP session manager serializes creation)
    for c in clients:
        await c.initialize()

    # Now fire heartbeats concurrently
    results = await asyncio.gather(
        *[c.call_tool("heartbeat", {}) for c in clients],
        return_exceptions=True,
    )
    for i, r in enumerate(results):
        assert not isinstance(r, Exception), f"peer{i} raised: {r}"
        assert r["ok"] is True, f"peer{i} got: {r}"


@pytest.mark.asyncio
async def test_concurrent_sends_to_same_channel(test_db, mcp_client):
    """5 peers send concurrently to the same channel. All messages preserved."""
    tokens = [await seed_peer(test_db, f"sender{i}") for i in range(5)]
    clients = [mcp_client(t, f"sender{i}") for i, t in enumerate(tokens)]
    for c in clients:
        await c.initialize()

    results = await asyncio.gather(
        *[
            c.call_tool("send", {"channel": "general", "content": f"msg from sender{i}"})
            for i, c in enumerate(clients)
        ]
    )
    for i, r in enumerate(results):
        assert r["ok"] is True, f"send {i} failed: {r}"

    # Verify all 5 messages are in the channel
    reader_tok = await seed_peer(test_db, "reader")
    reader = mcp_client(reader_tok, "reader")
    await reader.initialize()
    r = await reader.call_tool("receive", {"channel": "general"})
    assert r["ok"] is True
    assert r["count"] == 5
    contents = {m["content"] for m in r["messages"]}
    assert contents == {f"msg from sender{i}" for i in range(5)}


@pytest.mark.asyncio
async def test_concurrent_receives_cursor_no_regression(test_db, mcp_client):
    """Two receives with the same token but different sessions should not regress cursors.

    Simulates the scenario where the same peer has two Claude Code sessions
    reading simultaneously. The cursor upsert uses MAX() so the cursor
    advances monotonically even if a slower request commits after a faster one.
    """
    sender_tok = await seed_peer(test_db, "sender")
    reader_tok = await seed_peer(test_db, "reader")
    sender = mcp_client(sender_tok, "sender")
    # Two separate MCP clients sharing the same reader token
    reader1 = mcp_client(reader_tok, "reader1")
    reader2 = mcp_client(reader_tok, "reader2")
    await sender.initialize()
    await reader1.initialize()
    await reader2.initialize()

    # Send 20 messages
    for i in range(20):
        await sender.call_tool("send", {"channel": "general", "content": f"m{i}"})

    # reader1 reads all 20 -> cursor advances to 20
    r1 = await reader1.call_tool("receive", {"channel": "general", "limit": 20})
    assert r1["ok"] is True
    assert r1["count"] == 20
    assert r1["new_cursor"] == 20

    # reader2 attempts a receive with since_id=0 in peek mode (simulating a slow
    # request that retrieved earlier messages). Since peek=true, it should NOT
    # advance the cursor back.
    r2 = await reader2.call_tool("receive", {"channel": "general", "since_id": 5, "peek": True})
    assert r2["ok"] is True

    # Send 5 more messages (ids 21-25)
    for i in range(20, 25):
        await sender.call_tool("send", {"channel": "general", "content": f"m{i}"})

    # reader1 should only see the new 5 — cursor must still be at 20
    r3 = await reader1.call_tool("receive", {"channel": "general", "limit": 20})
    assert r3["ok"] is True
    contents = [m["content"] for m in r3["messages"]]
    assert len(contents) == 5
    for c in contents:
        assert c.startswith("m2"), f"unexpected old message: {c}"


@pytest.mark.asyncio
async def test_cursor_max_via_db_upsert(test_db):
    """Direct DB test: the MAX() upsert prevents cursor regression even if
    a stale request tries to write an older cursor value."""
    ns = "default"
    # Create a channel
    await test_db.execute(
        "INSERT INTO channels (namespace, name, created_by, created_at) "
        "VALUES (?, 'ch', 'alice', ?)",
        (ns, now_ms()),
    )
    ch = await test_db.fetchone(
        "SELECT channel_id FROM channels WHERE namespace=? AND name='ch'", (ns,)
    )
    ch_id = ch["channel_id"]

    # Set cursor to 50
    await test_db.execute(
        "INSERT INTO cursors (namespace, peer_name, channel_id, last_read_id) "
        "VALUES (?, 'alice', ?, 50) "
        "ON CONFLICT(namespace, peer_name, channel_id) "
        "DO UPDATE SET last_read_id = MAX(cursors.last_read_id, excluded.last_read_id)",
        (ns, ch_id),
    )

    # Try to regress to 30 — should stay at 50
    await test_db.execute(
        "INSERT INTO cursors (namespace, peer_name, channel_id, last_read_id) "
        "VALUES (?, 'alice', ?, 30) "
        "ON CONFLICT(namespace, peer_name, channel_id) "
        "DO UPDATE SET last_read_id = MAX(cursors.last_read_id, excluded.last_read_id)",
        (ns, ch_id),
    )
    row = await test_db.fetchone(
        "SELECT last_read_id FROM cursors WHERE namespace=? AND peer_name='alice' AND channel_id=?",
        (ns, ch_id),
    )
    assert row["last_read_id"] == 50

    # Advance to 80 — should advance
    await test_db.execute(
        "INSERT INTO cursors (namespace, peer_name, channel_id, last_read_id) "
        "VALUES (?, 'alice', ?, 80) "
        "ON CONFLICT(namespace, peer_name, channel_id) "
        "DO UPDATE SET last_read_id = MAX(cursors.last_read_id, excluded.last_read_id)",
        (ns, ch_id),
    )
    row = await test_db.fetchone(
        "SELECT last_read_id FROM cursors WHERE namespace=? AND peer_name='alice' AND channel_id=?",
        (ns, ch_id),
    )
    assert row["last_read_id"] == 80


# ------------------------------------------------------------------
# Stress
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_100_messages_in_channel(test_db, mcp_client):
    """Send 100 messages, retrieve them with pagination."""
    sender_tok = await seed_peer(test_db, "sender")
    reader_tok = await seed_peer(test_db, "reader")
    sender = mcp_client(sender_tok, "sender")
    reader = mcp_client(reader_tok, "reader")
    await sender.initialize()
    await reader.initialize()

    for i in range(100):
        r = await sender.call_tool("send", {"channel": "general", "content": f"msg-{i:03d}"})
        assert r["ok"] is True

    # Receive in batches of 20
    all_msgs = []
    for page in range(5):
        r = await reader.call_tool("receive", {"channel": "general", "limit": 20})
        assert r["ok"] is True
        all_msgs.extend(r["messages"])
        if page < 4:
            assert r["has_more"] is True or len(r["messages"]) == 20
        # Next batch
    assert len(all_msgs) == 100
    # Should be chronological
    contents = [m["content"] for m in all_msgs]
    assert contents == [f"msg-{i:03d}" for i in range(100)]


@pytest.mark.asyncio
async def test_large_message_at_max_size(test_db, mcp_client):
    """Send a message near the 50KB limit."""
    sender_tok = await seed_peer(test_db, "sender")
    reader_tok = await seed_peer(test_db, "reader")
    sender = mcp_client(sender_tok, "sender")
    reader = mcp_client(reader_tok, "reader")
    await sender.initialize()
    await reader.initialize()

    big = "x" * 49000
    r = await sender.call_tool("send", {"channel": "general", "content": big})
    assert r["ok"] is True

    r = await reader.call_tool("receive", {"channel": "general"})
    assert r["ok"] is True
    assert len(r["messages"]) == 1
    assert len(r["messages"][0]["content"]) == 49000


@pytest.mark.asyncio
async def test_message_exceeds_max_length_rejected(test_db, mcp_client):
    """Message over 50000 chars should be rejected by app validation."""
    tok = await seed_peer(test_db, "sender")
    client = mcp_client(tok, "sender")
    await client.initialize()

    too_big = "x" * 50001
    r = await client.call_tool("send", {"channel": "general", "content": too_big})
    assert r["ok"] is False
    assert "exceeds maximum length" in r["error"]
    assert "hint" in r


@pytest.mark.asyncio
async def test_response_size_cap_truncates(test_db, mcp_client):
    """Receive response should cap total size to prevent context overflow."""
    sender_tok = await seed_peer(test_db, "sender")
    reader_tok = await seed_peer(test_db, "reader")
    sender = mcp_client(sender_tok, "sender")
    reader = mcp_client(reader_tok, "reader")
    await sender.initialize()
    await reader.initialize()

    # Each message is ~10KB, 20 messages = 200KB total -> should hit 100KB cap
    chunk = "y" * 10000
    for _ in range(20):
        await sender.call_tool("send", {"channel": "general", "content": chunk})

    r = await reader.call_tool("receive", {"channel": "general", "limit": 20})
    assert r["ok"] is True
    # Should be capped at ~10 messages (100KB / 10KB each)
    assert len(r["messages"]) < 20
    assert r["has_more"] is True


@pytest.mark.asyncio
async def test_50_peers_each_send_one(test_db, mcp_client):
    """50 peers each send a single message. Sanity check the relay scales."""
    peers = []
    for i in range(50):
        tok = await seed_peer(test_db, f"p{i:02d}")
        c = mcp_client(tok, f"p{i:02d}")
        await c.initialize()
        peers.append(c)

    # Each peer sends once, concurrently
    await asyncio.gather(
        *[p.call_tool("send", {"channel": "general", "content": f"from p{i:02d}"})
          for i, p in enumerate(peers)]
    )

    # One peer reads them all
    r = await peers[0].call_tool("receive", {"channel": "general", "limit": 100})
    assert r["ok"] is True
    # Reader sees 50 messages (including its own)
    assert r["count"] == 50
    senders = {m["from"] for m in r["messages"]}
    assert len(senders) == 50


@pytest.mark.asyncio
async def test_many_channels_unread_summary(test_db, mcp_client):
    """Create many channels, verify heartbeat unread_summary is correct."""
    sender_tok = await seed_peer(test_db, "sender")
    reader_tok = await seed_peer(test_db, "reader")
    sender = mcp_client(sender_tok, "sender")
    reader = mcp_client(reader_tok, "reader")
    await sender.initialize()
    await reader.initialize()

    # Create 15 channels with 3 messages each
    for ch in range(15):
        for msg in range(3):
            await sender.call_tool("send", {
                "channel": f"ch-{ch:02d}",
                "content": f"msg {msg} in ch{ch}",
            })

    # Reader's heartbeat should report all unreads
    r = await reader.call_tool("heartbeat", {})
    assert r["ok"] is True
    summary = r["unread_summary"]
    assert summary["total_unread"] == 45
    assert len(summary["channels"]) == 15
    for entry in summary["channels"]:
        assert entry["unread"] == 3
