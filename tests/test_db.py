"""Tests for the database layer."""

import pytest
from relay import RelayDB, now_ms


@pytest.mark.asyncio
async def test_connect_creates_tables(test_db):
    """Schema should create all 5 tables."""
    tables = await test_db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    names = [t["name"] for t in tables]
    assert "channels" in names
    assert "cursors" in names
    assert "messages" in names
    assert "peers" in names
    assert "tokens" in names


@pytest.mark.asyncio
async def test_connect_creates_indices(test_db):
    """Schema should create indices."""
    indices = await test_db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
    )
    names = [i["name"] for i in indices]
    assert "idx_messages_channel_cursor" in names
    assert "idx_messages_namespace" in names
    assert "idx_peers_heartbeat" in names


@pytest.mark.asyncio
async def test_startup_resets_peers_offline(tmp_path):
    """On connect, all peers should be set to offline."""
    import hashlib
    db = RelayDB()
    db_path = str(tmp_path / "test_startup.db")
    await db.connect(db_path)

    # Insert a peer as online
    await db.execute(
        "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
        "VALUES (?, 'x', 'default', ?)",
        (hashlib.sha256(b"tok").hexdigest(), now_ms()),
    )
    await db.execute(
        "INSERT INTO peers (peer_name, namespace, status, last_heartbeat, first_seen) "
        "VALUES ('x', 'default', 'online', ?, ?)",
        (now_ms(), now_ms()),
    )
    await db.close()

    # Reconnect — should reset to offline
    db2 = RelayDB()
    await db2.connect(db_path)
    row = await db2.fetchone(
        "SELECT status FROM peers WHERE peer_name = 'x'"
    )
    assert row["status"] == "offline"
    await db2.close()


@pytest.mark.asyncio
async def test_execute_returns_cursor(test_db):
    """execute() should return a cursor with lastrowid."""
    cursor = await test_db.execute(
        "INSERT INTO channels (namespace, name, created_by, created_at) "
        "VALUES ('default', 'test', 'alice', ?)",
        (now_ms(),),
    )
    assert cursor.lastrowid is not None
    assert cursor.lastrowid > 0


@pytest.mark.asyncio
async def test_fetchone_returns_dict_or_none(test_db):
    """fetchone returns dict for existing row, None for missing."""
    result = await test_db.fetchone(
        "SELECT * FROM tokens WHERE token_hash = 'nonexistent'"
    )
    assert result is None

    await test_db.execute(
        "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
        "VALUES ('abc', 'test', 'default', ?)",
        (now_ms(),),
    )
    result = await test_db.fetchone(
        "SELECT * FROM tokens WHERE token_hash = 'abc'"
    )
    assert result is not None
    assert result["peer_name"] == "test"


@pytest.mark.asyncio
async def test_fetchall_returns_list(test_db):
    """fetchall returns list of dicts."""
    result = await test_db.fetchall("SELECT * FROM tokens")
    assert result == []

    await test_db.execute(
        "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
        "VALUES ('a', 'p1', 'default', ?)",
        (now_ms(),),
    )
    await test_db.execute(
        "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
        "VALUES ('b', 'p2', 'default', ?)",
        (now_ms(),),
    )
    result = await test_db.fetchall("SELECT * FROM tokens ORDER BY peer_name")
    assert len(result) == 2
    assert result[0]["peer_name"] == "p1"


@pytest.mark.asyncio
async def test_close_and_wal_checkpoint(test_db):
    """close() should checkpoint WAL and close connection."""
    await test_db.execute(
        "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
        "VALUES ('x', 'test', 'default', ?)",
        (now_ms(),),
    )
    await test_db.close()
    # After close, operations should fail
    with pytest.raises(AssertionError, match="DB not connected"):
        await test_db.fetchone("SELECT 1")


@pytest.mark.asyncio
async def test_message_content_check_constraint(test_db):
    """Empty content should be rejected by CHECK constraint."""
    import aiosqlite
    await test_db.execute(
        "INSERT INTO channels (namespace, name, created_by, created_at) "
        "VALUES ('default', 'ch', 'alice', ?)",
        (now_ms(),),
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await test_db.execute(
            "INSERT INTO messages (channel_id, sender_name, namespace, content, created_at) "
            "VALUES (1, 'alice', 'default', '', ?)",
            (now_ms(),),
        )


@pytest.mark.asyncio
async def test_channel_unique_constraint(test_db):
    """Duplicate (namespace, name) should be rejected."""
    import aiosqlite
    await test_db.execute(
        "INSERT INTO channels (namespace, name, created_by, created_at) "
        "VALUES ('default', 'general', 'alice', ?)",
        (now_ms(),),
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await test_db.execute(
            "INSERT INTO channels (namespace, name, created_by, created_at) "
            "VALUES ('default', 'general', 'bob', ?)",
            (now_ms(),),
        )


@pytest.mark.asyncio
async def test_cursor_max_prevents_regression(test_db):
    """Cursor upsert with MAX should not regress."""
    await test_db.execute(
        "INSERT INTO channels (namespace, name, created_by, created_at) "
        "VALUES ('default', 'ch', 'alice', ?)",
        (now_ms(),),
    )
    # Set cursor to 50
    await test_db.execute(
        "INSERT INTO cursors (namespace, peer_name, channel_id, last_read_id) "
        "VALUES ('default', 'alice', 1, 50)"
    )
    # Try to set to 30 (should stay at 50)
    await test_db.execute(
        "INSERT INTO cursors (namespace, peer_name, channel_id, last_read_id) "
        "VALUES ('default', 'alice', 1, 30) "
        "ON CONFLICT(namespace, peer_name, channel_id) "
        "DO UPDATE SET last_read_id = MAX(cursors.last_read_id, excluded.last_read_id)"
    )
    row = await test_db.fetchone(
        "SELECT last_read_id FROM cursors "
        "WHERE namespace='default' AND peer_name='alice' AND channel_id=1"
    )
    assert row["last_read_id"] == 50

    # Set to 60 (should advance)
    await test_db.execute(
        "INSERT INTO cursors (namespace, peer_name, channel_id, last_read_id) "
        "VALUES ('default', 'alice', 1, 60) "
        "ON CONFLICT(namespace, peer_name, channel_id) "
        "DO UPDATE SET last_read_id = MAX(cursors.last_read_id, excluded.last_read_id)"
    )
    row = await test_db.fetchone(
        "SELECT last_read_id FROM cursors "
        "WHERE namespace='default' AND peer_name='alice' AND channel_id=1"
    )
    assert row["last_read_id"] == 60
