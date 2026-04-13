"""Integration tests for the full relay flow.

Tests the complete message lifecycle: token creation, peer heartbeat,
message send/receive, cursor management, and multi-peer scenarios.
"""

import hashlib
import time

import pytest
from agentic_chat.db import RelayDB
from agentic_chat.config import now_ms, ms_to_iso
from agentic_chat.channels import normalize_channel


async def create_peer(db: RelayDB, name: str, ns: str = "default") -> str:
    """Create a token and peer. Returns raw token."""
    raw = f"relay_tok_integ_{name}_{time.monotonic()}"
    h = hashlib.sha256(raw.encode()).hexdigest()
    await db.execute(
        "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
        "VALUES (?, ?, ?, ?)",
        (h, name, ns, now_ms()),
    )
    await db.execute(
        "INSERT OR IGNORE INTO peers (peer_name, namespace, status, last_heartbeat, "
        "last_heartbeat_monotonic, first_seen) VALUES (?, ?, 'offline', ?, ?, ?)",
        (name, ns, now_ms(), time.monotonic(), now_ms()),
    )
    return raw


async def do_heartbeat(db: RelayDB, name: str, ns: str = "default",
                       status_msg: str | None = None):
    """Simulate a heartbeat for a peer."""
    now = now_ms()
    mono = time.monotonic()
    if status_msg:
        await db.execute(
            "UPDATE peers SET status='online', last_heartbeat=?, "
            "last_heartbeat_monotonic=?, status_message=? "
            "WHERE namespace=? AND peer_name=?",
            (now, mono, status_msg, ns, name),
        )
    else:
        await db.execute(
            "UPDATE peers SET status='online', last_heartbeat=?, "
            "last_heartbeat_monotonic=? WHERE namespace=? AND peer_name=?",
            (now, mono, ns, name),
        )


async def do_send(db: RelayDB, sender: str, channel: str, content: str,
                  ns: str = "default") -> int:
    """Simulate sending a message. Returns message_id."""
    channel, err = normalize_channel(channel)
    assert err is None, f"Channel normalization error: {err}"

    await db.execute(
        "INSERT OR IGNORE INTO channels (namespace, name, created_by, created_at) "
        "VALUES (?, ?, ?, ?)",
        (ns, channel, sender, now_ms()),
    )
    ch = await db.fetchone(
        "SELECT channel_id FROM channels WHERE namespace=? AND name=?",
        (ns, channel),
    )
    cursor = await db.execute(
        "INSERT INTO messages (channel_id, sender_name, namespace, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ch["channel_id"], sender, ns, content, now_ms()),
    )
    return cursor.lastrowid


async def do_receive(db: RelayDB, peer: str, channel: str | None = None,
                     ns: str = "default", limit: int = 20) -> list[dict]:
    """Simulate receiving messages. Updates cursors. Returns messages."""
    if channel is None:
        rows = await db.fetchall(
            """SELECT m.message_id, m.sender_name, m.content, c.name AS channel_name,
                      m.channel_id
               FROM messages m
               JOIN channels c ON c.channel_id = m.channel_id
               LEFT JOIN cursors cu ON cu.channel_id = m.channel_id
                   AND cu.namespace = ? AND cu.peer_name = ?
               WHERE m.namespace = ?
                 AND m.message_id > COALESCE(cu.last_read_id, 0)
               ORDER BY m.message_id ASC LIMIT ?""",
            (ns, peer, ns, limit),
        )
        # Update cursors per channel
        channel_max: dict[int, int] = {}
        for r in rows:
            ch_id = r["channel_id"]
            if ch_id not in channel_max or r["message_id"] > channel_max[ch_id]:
                channel_max[ch_id] = r["message_id"]
        for ch_id, max_id in channel_max.items():
            await db.execute(
                "INSERT INTO cursors (namespace, peer_name, channel_id, last_read_id) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(namespace, peer_name, channel_id) "
                "DO UPDATE SET last_read_id = MAX(cursors.last_read_id, excluded.last_read_id)",
                (ns, peer, ch_id, max_id),
            )
        return [dict(r) for r in rows]
    else:
        channel, _ = normalize_channel(channel)
        ch = await db.fetchone(
            "SELECT channel_id FROM channels WHERE namespace=? AND name=?",
            (ns, channel),
        )
        if not ch:
            return []
        channel_id = ch["channel_id"]
        cursor_row = await db.fetchone(
            "SELECT last_read_id FROM cursors "
            "WHERE namespace=? AND peer_name=? AND channel_id=?",
            (ns, peer, channel_id),
        )
        start = cursor_row["last_read_id"] if cursor_row else 0
        rows = await db.fetchall(
            "SELECT message_id, sender_name, content FROM messages "
            "WHERE channel_id=? AND namespace=? AND message_id > ? "
            "ORDER BY message_id ASC LIMIT ?",
            (channel_id, ns, start, limit),
        )
        if rows:
            max_id = max(r["message_id"] for r in rows)
            await db.execute(
                "INSERT INTO cursors (namespace, peer_name, channel_id, last_read_id) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(namespace, peer_name, channel_id) "
                "DO UPDATE SET last_read_id = MAX(cursors.last_read_id, excluded.last_read_id)",
                (ns, peer, channel_id, max_id),
            )
        return [dict(r) for r in rows]


# -- Full lifecycle tests --


@pytest.mark.asyncio
async def test_full_lifecycle(test_db):
    """Complete flow: create peers, heartbeat, send, receive, cursor advance."""
    db = test_db
    await create_peer(db, "alice")
    await create_peer(db, "bob")

    # Heartbeat
    await do_heartbeat(db, "alice", status_msg="coding")
    await do_heartbeat(db, "bob")

    peers = await db.fetchall(
        "SELECT peer_name, status FROM peers WHERE namespace='default' ORDER BY peer_name"
    )
    assert len(peers) == 2
    assert all(p["status"] == "online" for p in peers)

    # Alice sends to general
    msg1 = await do_send(db, "alice", "general", "Hello everyone!")
    msg2 = await do_send(db, "alice", "general", "Anyone online?")

    # Alice sends DM to bob
    msg3 = await do_send(db, "alice", "dm-alice-bob", "Hey bob, private msg")

    # Bob receives all
    messages = await do_receive(db, "bob")
    assert len(messages) == 3
    assert messages[0]["content"] == "Hello everyone!"
    assert messages[1]["content"] == "Anyone online?"
    assert messages[2]["content"] == "Hey bob, private msg"
    assert messages[2]["channel_name"] == "dm-alice-bob"

    # Bob receives again -> empty (cursors advanced)
    messages = await do_receive(db, "bob")
    assert len(messages) == 0

    # Alice sends more
    msg4 = await do_send(db, "alice", "general", "New update!")

    # Bob receives -> only the new one
    messages = await do_receive(db, "bob")
    assert len(messages) == 1
    assert messages[0]["content"] == "New update!"


@pytest.mark.asyncio
async def test_dm_normalization_end_to_end(test_db):
    """DM channel names should be normalized regardless of order."""
    db = test_db
    await create_peer(db, "alice")
    await create_peer(db, "bob")

    # Send as dm-bob-alice
    msg1 = await do_send(db, "alice", "dm-bob-alice", "msg via bob-alice")
    # Send as dm-alice-bob
    msg2 = await do_send(db, "bob", "dm-alice-bob", "msg via alice-bob")

    # Both should be in the same channel
    channels = await db.fetchall(
        "SELECT name FROM channels WHERE namespace='default' AND name LIKE 'dm-%'"
    )
    assert len(channels) == 1
    assert channels[0]["name"] == "dm-alice-bob"

    messages = await do_receive(db, "alice", "dm-alice-bob")
    assert len(messages) == 2


@pytest.mark.asyncio
async def test_multi_channel_receive(test_db):
    """All-channels receive should interleave messages chronologically."""
    db = test_db
    await create_peer(db, "alice")
    await create_peer(db, "bob")
    await create_peer(db, "carol")

    await do_send(db, "alice", "general", "gen 1")
    await do_send(db, "bob", "random", "rand 1")
    await do_send(db, "carol", "general", "gen 2")
    await do_send(db, "alice", "random", "rand 2")

    # Carol receives all channels
    messages = await do_receive(db, "carol")
    assert len(messages) == 4
    # Should be in message_id order
    ids = [m["message_id"] for m in messages]
    assert ids == sorted(ids)


@pytest.mark.asyncio
async def test_namespace_full_isolation(test_db):
    """Peers in different namespaces should be completely isolated."""
    db = test_db
    await create_peer(db, "alice", "team-a")
    await create_peer(db, "bob", "team-b")

    await do_send(db, "alice", "general", "team A secret", "team-a")
    await do_send(db, "bob", "general", "team B secret", "team-b")

    # Alice in team-a should only see team-a messages
    msgs_a = await do_receive(db, "alice", "general", "team-a")
    assert len(msgs_a) == 1
    assert msgs_a[0]["content"] == "team A secret"

    # Bob in team-b should only see team-b messages
    msgs_b = await do_receive(db, "bob", "general", "team-b")
    assert len(msgs_b) == 1
    assert msgs_b[0]["content"] == "team B secret"


@pytest.mark.asyncio
async def test_token_revoke_and_recreate(test_db):
    """Revoking and recreating a token should work cleanly."""
    db = test_db
    raw1 = await create_peer(db, "alice")
    h1 = hashlib.sha256(raw1.encode()).hexdigest()

    # Send a message and advance cursor
    await do_send(db, "alice", "general", "hello")
    await do_receive(db, "alice", "general")

    # Verify cursor exists
    cursor = await db.fetchone(
        "SELECT last_read_id FROM cursors "
        "WHERE namespace='default' AND peer_name='alice'"
    )
    assert cursor is not None

    # Revoke
    await db.execute(
        "DELETE FROM tokens WHERE peer_name='alice' AND namespace='default'"
    )
    await db.execute(
        "DELETE FROM cursors WHERE peer_name='alice' AND namespace='default'"
    )

    # Verify token gone
    row = await db.fetchone(
        "SELECT * FROM tokens WHERE token_hash=?", (h1,)
    )
    assert row is None

    # Verify cursors cleaned
    cursor = await db.fetchone(
        "SELECT * FROM cursors WHERE peer_name='alice' AND namespace='default'"
    )
    assert cursor is None

    # Recreate
    raw2 = await create_peer(db, "alice")
    h2 = hashlib.sha256(raw2.encode()).hexdigest()

    row = await db.fetchone(
        "SELECT peer_name FROM tokens WHERE token_hash=?", (h2,)
    )
    assert row["peer_name"] == "alice"


@pytest.mark.asyncio
async def test_large_message_handling(test_db):
    """Messages up to max length should be stored correctly."""
    db = test_db
    await create_peer(db, "alice")
    await create_peer(db, "bob")

    big_content = "x" * 49999  # just under 50KB
    msg_id = await do_send(db, "alice", "general", big_content)
    assert msg_id > 0

    msgs = await do_receive(db, "bob", "general")
    assert len(msgs) == 1
    assert len(msgs[0]["content"]) == 49999


@pytest.mark.asyncio
async def test_many_messages_limit(test_db):
    """Receive with limit should respect the limit."""
    db = test_db
    await create_peer(db, "alice")
    await create_peer(db, "bob")

    for i in range(30):
        await do_send(db, "alice", "general", f"msg {i}")

    # Receive with limit 10
    msgs = await do_receive(db, "bob", "general", limit=10)
    assert len(msgs) == 10
    assert msgs[0]["content"] == "msg 0"
    assert msgs[9]["content"] == "msg 9"

    # Receive again -> next 10
    msgs = await do_receive(db, "bob", "general", limit=10)
    assert len(msgs) == 10
    assert msgs[0]["content"] == "msg 10"


@pytest.mark.asyncio
async def test_concurrent_cursor_no_regression(test_db):
    """Concurrent receives should not regress cursor (MAX protection)."""
    db = test_db
    await create_peer(db, "alice")
    await create_peer(db, "bob")

    ch_id_row = None
    for i in range(5):
        msg_id = await do_send(db, "alice", "general", f"msg {i}")

    ch_id_row = await db.fetchone(
        "SELECT channel_id FROM channels WHERE namespace='default' AND name='general'"
    )
    ch_id = ch_id_row["channel_id"]

    # Advance cursor to message 5
    await db.execute(
        "INSERT INTO cursors (namespace, peer_name, channel_id, last_read_id) "
        "VALUES ('default', 'bob', ?, 5) "
        "ON CONFLICT(namespace, peer_name, channel_id) "
        "DO UPDATE SET last_read_id = MAX(cursors.last_read_id, excluded.last_read_id)",
        (ch_id,),
    )

    # Try to set cursor back to 3 (simulating a stale concurrent request)
    await db.execute(
        "INSERT INTO cursors (namespace, peer_name, channel_id, last_read_id) "
        "VALUES ('default', 'bob', ?, 3) "
        "ON CONFLICT(namespace, peer_name, channel_id) "
        "DO UPDATE SET last_read_id = MAX(cursors.last_read_id, excluded.last_read_id)",
        (ch_id,),
    )

    # Cursor should still be at 5
    row = await db.fetchone(
        "SELECT last_read_id FROM cursors "
        "WHERE namespace='default' AND peer_name='bob' AND channel_id=?",
        (ch_id,),
    )
    assert row["last_read_id"] == 5


@pytest.mark.asyncio
async def test_empty_channel_receive(test_db):
    """Receiving from an empty channel should return empty list."""
    db = test_db
    await create_peer(db, "alice")

    # Create an empty channel
    await db.execute(
        "INSERT INTO channels (namespace, name, created_by, created_at) "
        "VALUES ('default', 'empty', 'alice', ?)",
        (now_ms(),),
    )

    msgs = await do_receive(db, "alice", "empty")
    assert msgs == []
