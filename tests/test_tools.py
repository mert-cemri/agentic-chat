"""Tests for MCP tool handlers -- unit tests using direct DB calls.

These tests call the tool logic directly against the DB without going through
MCP protocol. They test the SQL queries, business logic, and return formats.
"""

import hashlib
import time

import pytest
from agentic_chat.db import RelayDB
from agentic_chat.config import now_ms, ms_to_iso, CONFIG, DEFAULT_CONFIG
from agentic_chat.channels import normalize_channel


# -- Helper to simulate a tool call context --


async def setup_channel(db: RelayDB, ns: str, name: str, creator: str) -> int:
    """Create a channel and return its channel_id."""
    await db.execute(
        "INSERT OR IGNORE INTO channels (namespace, name, created_by, created_at) "
        "VALUES (?, ?, ?, ?)",
        (ns, name, creator, now_ms()),
    )
    row = await db.fetchone(
        "SELECT channel_id FROM channels WHERE namespace = ? AND name = ?",
        (ns, name),
    )
    return row["channel_id"]


async def send_message(db: RelayDB, ns: str, channel_id: int, sender: str, content: str) -> int:
    """Insert a message and return its message_id."""
    cursor = await db.execute(
        "INSERT INTO messages (channel_id, sender_name, namespace, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (channel_id, sender, ns, content, now_ms()),
    )
    return cursor.lastrowid


# -- Heartbeat logic tests --


@pytest.mark.asyncio
async def test_heartbeat_updates_status(seeded_db):
    """Heartbeat should set peer to online and update timestamps."""
    db, _ = seeded_db
    now = now_ms()
    mono = time.monotonic()

    await db.execute(
        "UPDATE peers SET status='online', last_heartbeat=?, last_heartbeat_monotonic=? "
        "WHERE namespace='default' AND peer_name='alice'",
        (now, mono),
    )

    row = await db.fetchone(
        "SELECT status, last_heartbeat FROM peers "
        "WHERE namespace='default' AND peer_name='alice'"
    )
    assert row["status"] == "online"
    assert row["last_heartbeat"] == now


@pytest.mark.asyncio
async def test_heartbeat_status_message(seeded_db):
    """Heartbeat with status_message should update it."""
    db, _ = seeded_db
    await db.execute(
        "UPDATE peers SET status='online', status_message='working on auth' "
        "WHERE namespace='default' AND peer_name='alice'"
    )
    row = await db.fetchone(
        "SELECT status_message FROM peers "
        "WHERE namespace='default' AND peer_name='alice'"
    )
    assert row["status_message"] == "working on auth"


@pytest.mark.asyncio
async def test_stale_peer_detection(seeded_db):
    """Peers with old heartbeats should be marked offline."""
    db, _ = seeded_db
    old_mono = time.monotonic() - 200  # 200s ago, past 120s timeout

    await db.execute(
        "UPDATE peers SET status='online', last_heartbeat_monotonic=? "
        "WHERE namespace='default' AND peer_name='alice'",
        (old_mono,),
    )

    timeout = 120
    cutoff = time.monotonic() - timeout
    await db.execute(
        "UPDATE peers SET status='offline' "
        "WHERE namespace='default' AND status='online' AND last_heartbeat_monotonic < ?",
        (cutoff,),
    )

    row = await db.fetchone(
        "SELECT status FROM peers WHERE namespace='default' AND peer_name='alice'"
    )
    assert row["status"] == "offline"


@pytest.mark.asyncio
async def test_unread_count_query(seeded_db):
    """Unread count query should return channels with unread messages."""
    db, _ = seeded_db
    ns = "default"

    ch_id = await setup_channel(db, ns, "general", "alice")
    await send_message(db, ns, ch_id, "alice", "hello")
    await send_message(db, ns, ch_id, "alice", "world")

    # Bob has no cursor -> should see 2 unread
    rows = await db.fetchall(
        """SELECT c.name AS channel, COUNT(m.message_id) AS unread
           FROM channels c
           JOIN messages m ON m.channel_id = c.channel_id
           LEFT JOIN cursors cu ON cu.channel_id = c.channel_id
               AND cu.namespace = ? AND cu.peer_name = ?
           WHERE c.namespace = ?
             AND m.message_id > COALESCE(cu.last_read_id, 0)
           GROUP BY c.channel_id HAVING unread > 0""",
        (ns, "bob", ns),
    )
    assert len(rows) == 1
    assert rows[0]["channel"] == "general"
    assert rows[0]["unread"] == 2


# -- Send logic tests --


@pytest.mark.asyncio
async def test_send_creates_channel(seeded_db):
    """Sending to a new channel should auto-create it."""
    db, _ = seeded_db
    ns = "default"

    await db.execute(
        "INSERT OR IGNORE INTO channels (namespace, name, created_by, created_at) "
        "VALUES (?, 'newchan', 'alice', ?)",
        (ns, now_ms()),
    )

    row = await db.fetchone(
        "SELECT * FROM channels WHERE namespace = ? AND name = 'newchan'", (ns,)
    )
    assert row is not None
    assert row["created_by"] == "alice"


@pytest.mark.asyncio
async def test_send_insert_message(seeded_db):
    """Messages should be inserted with correct fields."""
    db, _ = seeded_db
    ns = "default"

    ch_id = await setup_channel(db, ns, "general", "alice")
    msg_id = await send_message(db, ns, ch_id, "alice", "hello world")
    assert msg_id > 0

    row = await db.fetchone(
        "SELECT * FROM messages WHERE message_id = ?", (msg_id,)
    )
    assert row["sender_name"] == "alice"
    assert row["namespace"] == ns
    assert row["content"] == "hello world"


@pytest.mark.asyncio
async def test_dm_normalization_in_send(seeded_db):
    """DM channels should be normalized when creating."""
    db, _ = seeded_db
    ns = "default"

    # Create via "dm-bob-alice" -> should store as "dm-alice-bob"
    channel, err = normalize_channel("dm-bob-alice")
    assert err is None
    assert channel == "dm-alice-bob"

    await db.execute(
        "INSERT OR IGNORE INTO channels (namespace, name, created_by, created_at) "
        "VALUES (?, ?, 'alice', ?)",
        (ns, channel, now_ms()),
    )

    row = await db.fetchone(
        "SELECT * FROM channels WHERE namespace = ? AND name = 'dm-alice-bob'", (ns,)
    )
    assert row is not None


# -- Receive logic tests --


@pytest.mark.asyncio
async def test_receive_single_channel(seeded_db):
    """Receive should return messages after cursor."""
    db, _ = seeded_db
    ns = "default"

    ch_id = await setup_channel(db, ns, "general", "alice")
    id1 = await send_message(db, ns, ch_id, "alice", "msg 1")
    id2 = await send_message(db, ns, ch_id, "alice", "msg 2")
    id3 = await send_message(db, ns, ch_id, "bob", "msg 3")

    # Bob reads from cursor 0
    rows = await db.fetchall(
        "SELECT message_id, sender_name, content FROM messages "
        "WHERE channel_id = ? AND namespace = ? AND message_id > ? "
        "ORDER BY message_id ASC LIMIT 20",
        (ch_id, ns, 0),
    )
    assert len(rows) == 3
    assert rows[0]["content"] == "msg 1"
    assert rows[2]["content"] == "msg 3"


@pytest.mark.asyncio
async def test_receive_cursor_advance(seeded_db):
    """After receive, cursor should advance to max message_id."""
    db, _ = seeded_db
    ns = "default"

    ch_id = await setup_channel(db, ns, "general", "alice")
    id1 = await send_message(db, ns, ch_id, "alice", "msg 1")
    id2 = await send_message(db, ns, ch_id, "alice", "msg 2")

    # Set cursor for bob
    await db.execute(
        "INSERT INTO cursors (namespace, peer_name, channel_id, last_read_id) "
        "VALUES ('default', 'bob', ?, ?) "
        "ON CONFLICT(namespace, peer_name, channel_id) "
        "DO UPDATE SET last_read_id = MAX(cursors.last_read_id, excluded.last_read_id)",
        (ch_id, id2),
    )

    # Add new message
    id3 = await send_message(db, ns, ch_id, "alice", "msg 3")

    # Bob should only see msg 3
    cursor_row = await db.fetchone(
        "SELECT last_read_id FROM cursors "
        "WHERE namespace='default' AND peer_name='bob' AND channel_id=?",
        (ch_id,),
    )
    rows = await db.fetchall(
        "SELECT message_id, content FROM messages "
        "WHERE channel_id = ? AND namespace = ? AND message_id > ? "
        "ORDER BY message_id ASC LIMIT 20",
        (ch_id, ns, cursor_row["last_read_id"]),
    )
    assert len(rows) == 1
    assert rows[0]["content"] == "msg 3"


@pytest.mark.asyncio
async def test_receive_peek_no_cursor_advance(seeded_db):
    """Peek mode should not advance the cursor."""
    db, _ = seeded_db
    ns = "default"

    ch_id = await setup_channel(db, ns, "general", "alice")
    await send_message(db, ns, ch_id, "alice", "msg 1")

    # No cursor set -> should be None
    cursor_row = await db.fetchone(
        "SELECT last_read_id FROM cursors "
        "WHERE namespace='default' AND peer_name='bob' AND channel_id=?",
        (ch_id,),
    )
    assert cursor_row is None  # no cursor yet


@pytest.mark.asyncio
async def test_receive_all_channels(seeded_db):
    """All-channels mode should return messages from all channels sorted by ID."""
    db, _ = seeded_db
    ns = "default"

    ch1 = await setup_channel(db, ns, "general", "alice")
    ch2 = await setup_channel(db, ns, "random", "bob")

    id1 = await send_message(db, ns, ch1, "alice", "gen msg")
    id2 = await send_message(db, ns, ch2, "bob", "random msg")
    id3 = await send_message(db, ns, ch1, "alice", "gen msg 2")

    rows = await db.fetchall(
        """SELECT m.message_id, m.sender_name, m.content, c.name AS channel_name
           FROM messages m
           JOIN channels c ON c.channel_id = m.channel_id
           LEFT JOIN cursors cu
               ON cu.channel_id = m.channel_id
               AND cu.namespace = ? AND cu.peer_name = ?
           WHERE m.namespace = ?
             AND m.message_id > COALESCE(cu.last_read_id, 0)
           ORDER BY m.message_id ASC
           LIMIT 20""",
        (ns, "carol", ns),
    )
    assert len(rows) == 3
    assert rows[0]["channel_name"] == "general"
    assert rows[1]["channel_name"] == "random"
    assert rows[2]["channel_name"] == "general"


@pytest.mark.asyncio
async def test_since_id_returns_historical(seeded_db):
    """since_id should return messages after that ID."""
    db, _ = seeded_db
    ns = "default"

    ch_id = await setup_channel(db, ns, "general", "alice")
    id1 = await send_message(db, ns, ch_id, "alice", "old msg")
    id2 = await send_message(db, ns, ch_id, "alice", "new msg")

    # since_id=0 returns all
    rows = await db.fetchall(
        "SELECT content FROM messages WHERE channel_id=? AND namespace=? AND message_id > ? "
        "ORDER BY message_id ASC",
        (ch_id, ns, 0),
    )
    assert len(rows) == 2

    # since_id=id1 returns only new msg
    rows = await db.fetchall(
        "SELECT content FROM messages WHERE channel_id=? AND namespace=? AND message_id > ? "
        "ORDER BY message_id ASC",
        (ch_id, ns, id1),
    )
    assert len(rows) == 1
    assert rows[0]["content"] == "new msg"


# -- Namespace isolation tests --


@pytest.mark.asyncio
async def test_namespace_isolation_messages(seeded_db):
    """Messages in namespace A should not be visible in namespace B queries."""
    db, _ = seeded_db

    ch_a = await setup_channel(db, "ns-a", "general", "alice")
    ch_b = await setup_channel(db, "ns-b", "general", "bob")

    await send_message(db, "ns-a", ch_a, "alice", "secret A")
    await send_message(db, "ns-b", ch_b, "bob", "secret B")

    # Query ns-a -> should only see A's message
    rows = await db.fetchall(
        "SELECT content FROM messages WHERE namespace = ?", ("ns-a",)
    )
    assert len(rows) == 1
    assert rows[0]["content"] == "secret A"


@pytest.mark.asyncio
async def test_namespace_isolation_peers(seeded_db):
    """Peers in different namespaces should not see each other."""
    db, _ = seeded_db

    # alice in ns-a, dave in ns-b
    await db.execute(
        "INSERT INTO tokens (token_hash, owner_name, namespace, created_at) "
        "VALUES ('dave_hash', 'dave', 'ns-b', ?)",
        (now_ms(),),
    )
    await db.execute(
        "INSERT INTO peers (peer_name, namespace, status, first_seen) "
        "VALUES ('dave', 'ns-b', 'online', ?)",
        (now_ms(),),
    )

    # Query peers in default namespace
    peers = await db.fetchall(
        "SELECT peer_name FROM peers WHERE namespace = 'default'"
    )
    names = [p["peer_name"] for p in peers]
    assert "dave" not in names
    assert "alice" in names


# -- List channels tests --


@pytest.mark.asyncio
async def test_list_channels_with_unread(seeded_db):
    """list_channels should compute unread counts correctly."""
    db, _ = seeded_db
    ns = "default"

    ch_id = await setup_channel(db, ns, "general", "alice")
    await send_message(db, ns, ch_id, "alice", "msg 1")
    await send_message(db, ns, ch_id, "alice", "msg 2")

    rows = await db.fetchall(
        """SELECT c.name,
                  COUNT(CASE WHEN m.message_id > COALESCE(cu.last_read_id, 0) THEN 1 END) AS unread
           FROM channels c
           LEFT JOIN messages m ON m.channel_id = c.channel_id AND m.namespace = ?
           LEFT JOIN cursors cu ON cu.channel_id = c.channel_id
               AND cu.namespace = ? AND cu.peer_name = ?
           WHERE c.namespace = ?
           GROUP BY c.channel_id""",
        (ns, ns, "bob", ns),
    )
    assert len(rows) == 1
    assert rows[0]["unread"] == 2


# -- Cleanup tests --


@pytest.mark.asyncio
async def test_cleanup_deletes_old_messages(seeded_db):
    """Messages older than retention should be deleted."""
    db, _ = seeded_db
    ns = "default"

    ch_id = await setup_channel(db, ns, "general", "alice")

    # Insert old message (8 days ago)
    old_ts = now_ms() - (8 * 86400 * 1000)
    await db.execute(
        "INSERT INTO messages (channel_id, sender_name, namespace, content, created_at) "
        "VALUES (?, 'alice', ?, 'old msg', ?)",
        (ch_id, ns, old_ts),
    )
    # Insert recent message
    await send_message(db, ns, ch_id, "alice", "new msg")

    # Run cleanup (7 day retention)
    cutoff = now_ms() - (7 * 86400 * 1000)
    await db.execute(
        "DELETE FROM messages WHERE rowid IN "
        "(SELECT rowid FROM messages WHERE created_at < ? LIMIT 5000)",
        (cutoff,),
    )

    rows = await db.fetchall(
        "SELECT content FROM messages WHERE namespace = ?", (ns,)
    )
    assert len(rows) == 1
    assert rows[0]["content"] == "new msg"
