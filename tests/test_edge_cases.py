"""Edge case tests: unicode, boundaries, unusual flows, recovery scenarios."""

import hashlib

import pytest
from relay import now_ms


async def seed_peer(db, name: str, ns: str = "default") -> str:
    raw = f"relay_tok_edge_{name}"
    h = hashlib.sha256(raw.encode()).hexdigest()
    await db.execute(
        "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
        "VALUES (?, ?, ?, ?)",
        (h, name, ns, now_ms()),
    )
    return raw


# ------------------------------------------------------------------
# Unicode
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unicode_messages(test_db, mcp_client):
    """Emoji, CJK, RTL, and other unicode should roundtrip correctly."""
    sender_tok = await seed_peer(test_db, "sender")
    reader_tok = await seed_peer(test_db, "reader")
    sender = mcp_client(sender_tok, "sender")
    reader = mcp_client(reader_tok, "reader")
    await sender.initialize()
    await reader.initialize()

    test_messages = [
        "Hello, world!",
        "שלום עולם",  # Hebrew (RTL)
        "こんにちは世界",  # Japanese
        "你好，世界",  # Chinese
        "مرحبا بالعالم",  # Arabic (RTL)
        "Привет, мир",  # Cyrillic
        "🚀🎉👋 emoji test 🌈",
        "Mixed: Hello 世界 🌍 שלום",
        "Math: ∀x∈ℝ: x²≥0",
        "Code: `const x = { foo: 'bar' };`",
    ]
    for msg in test_messages:
        r = await sender.call_tool("send", {"channel": "unicode-test", "content": msg})
        assert r["ok"] is True, f"failed to send {msg!r}"

    r = await reader.call_tool("receive", {"channel": "unicode-test", "limit": 100})
    assert r["ok"] is True
    received = [m["content"] for m in r["messages"]]
    assert received == test_messages


@pytest.mark.asyncio
async def test_unicode_status_message(test_db, mcp_client):
    """Status messages with unicode should work."""
    tok = await seed_peer(test_db, "alice")
    client = mcp_client(tok, "alice")
    await client.initialize()

    status = "🔥 deep in a refactor 🛠️"
    r = await client.call_tool("heartbeat", {"status_message": status})
    assert r["ok"] is True

    r = await client.call_tool("list_peers", {})
    alice_row = next(p for p in r["peers"] if p["name"] == "alice")
    assert alice_row["status_message"] == status


# ------------------------------------------------------------------
# Boundary conditions
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_character_message(test_db, mcp_client):
    """A 1-character message is valid."""
    tok = await seed_peer(test_db, "alice")
    client = mcp_client(tok, "alice")
    await client.initialize()

    r = await client.call_tool("send", {"channel": "general", "content": "x"})
    assert r["ok"] is True


@pytest.mark.asyncio
async def test_exact_max_message_length(test_db, mcp_client):
    """Message at exactly 50000 characters should be accepted."""
    sender_tok = await seed_peer(test_db, "sender")
    reader_tok = await seed_peer(test_db, "reader")
    sender = mcp_client(sender_tok, "sender")
    reader = mcp_client(reader_tok, "reader")
    await sender.initialize()
    await reader.initialize()

    content = "a" * 50000
    r = await sender.call_tool("send", {"channel": "general", "content": content})
    assert r["ok"] is True

    r = await reader.call_tool("receive", {"channel": "general"})
    assert r["ok"] is True
    assert len(r["messages"][0]["content"]) == 50000


@pytest.mark.asyncio
async def test_channel_name_boundaries(test_db, mcp_client):
    """Channel names at regex boundaries."""
    tok = await seed_peer(test_db, "alice")
    client = mcp_client(tok, "alice")
    await client.initialize()

    # 1 char (min)
    r = await client.call_tool("send", {"channel": "a", "content": "x"})
    assert r["ok"] is True

    # 64 chars (max)
    r = await client.call_tool("send", {"channel": "a" * 64, "content": "x"})
    assert r["ok"] is True

    # 65 chars (over max)
    r = await client.call_tool("send", {"channel": "a" * 65, "content": "x"})
    assert r["ok"] is False

    # Starts with hyphen (bad)
    r = await client.call_tool("send", {"channel": "-foo", "content": "x"})
    assert r["ok"] is False

    # Starts with digit (ok)
    r = await client.call_tool("send", {"channel": "1foo", "content": "x"})
    assert r["ok"] is True


@pytest.mark.asyncio
async def test_limit_at_boundaries(test_db, mcp_client):
    """Receive limit at exactly 1 and 100."""
    sender_tok = await seed_peer(test_db, "sender")
    reader_tok = await seed_peer(test_db, "reader")
    sender = mcp_client(sender_tok, "sender")
    reader = mcp_client(reader_tok, "reader")
    await sender.initialize()
    await reader.initialize()

    for i in range(5):
        await sender.call_tool("send", {"channel": "general", "content": f"m{i}"})

    # limit=1
    r = await reader.call_tool("receive", {"channel": "general", "limit": 1})
    assert r["ok"] is True
    assert r["count"] == 1

    # limit=100 (more than messages)
    r = await reader.call_tool("receive", {"channel": "general", "limit": 100})
    assert r["ok"] is True
    assert r["count"] == 4  # remaining


# ------------------------------------------------------------------
# since_id edge cases
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_id_zero_returns_all(test_db, mcp_client):
    """since_id=0 returns everything from the start."""
    sender_tok = await seed_peer(test_db, "sender")
    reader_tok = await seed_peer(test_db, "reader")
    sender = mcp_client(sender_tok, "sender")
    reader = mcp_client(reader_tok, "reader")
    await sender.initialize()
    await reader.initialize()

    for i in range(3):
        await sender.call_tool("send", {"channel": "general", "content": f"m{i}"})

    # Read and advance cursor
    r = await reader.call_tool("receive", {"channel": "general"})
    assert r["count"] == 3

    # Now use since_id=0 to re-read everything
    r = await reader.call_tool("receive", {"channel": "general", "since_id": 0})
    assert r["count"] == 3  # got them all back


@pytest.mark.asyncio
async def test_since_id_does_not_advance_cursor(test_db, mcp_client):
    """since_id reads should not advance the stored cursor."""
    sender_tok = await seed_peer(test_db, "sender")
    reader_tok = await seed_peer(test_db, "reader")
    sender = mcp_client(sender_tok, "sender")
    reader = mcp_client(reader_tok, "reader")
    await sender.initialize()
    await reader.initialize()

    for i in range(5):
        await sender.call_tool("send", {"channel": "general", "content": f"m{i}"})

    # Use since_id=0 — does NOT advance cursor
    r = await reader.call_tool("receive", {"channel": "general", "since_id": 0})
    assert r["count"] == 5

    # Normal receive — should still return all 5 (cursor still at 0)
    r = await reader.call_tool("receive", {"channel": "general"})
    assert r["count"] == 5

    # Normal receive again — now empty
    r = await reader.call_tool("receive", {"channel": "general"})
    assert r["count"] == 0


@pytest.mark.asyncio
async def test_since_id_beyond_max_returns_empty(test_db, mcp_client):
    """since_id beyond max message_id returns empty without error."""
    sender_tok = await seed_peer(test_db, "sender")
    reader_tok = await seed_peer(test_db, "reader")
    sender = mcp_client(sender_tok, "sender")
    reader = mcp_client(reader_tok, "reader")
    await sender.initialize()
    await reader.initialize()

    await sender.call_tool("send", {"channel": "general", "content": "m1"})

    r = await reader.call_tool("receive", {"channel": "general", "since_id": 9999999})
    assert r["ok"] is True
    assert r["count"] == 0


# ------------------------------------------------------------------
# Peek mode
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peek_does_not_advance_cursor(test_db, mcp_client):
    """peek=true should leave the cursor unchanged."""
    sender_tok = await seed_peer(test_db, "sender")
    reader_tok = await seed_peer(test_db, "reader")
    sender = mcp_client(sender_tok, "sender")
    reader = mcp_client(reader_tok, "reader")
    await sender.initialize()
    await reader.initialize()

    await sender.call_tool("send", {"channel": "general", "content": "m1"})
    await sender.call_tool("send", {"channel": "general", "content": "m2"})

    # Peek
    r = await reader.call_tool("receive", {"channel": "general", "peek": True})
    assert r["count"] == 2

    # Peek again — still 2
    r = await reader.call_tool("receive", {"channel": "general", "peek": True})
    assert r["count"] == 2

    # Normal read — still 2 (cursor didn't advance during peeks)
    r = await reader.call_tool("receive", {"channel": "general"})
    assert r["count"] == 2

    # Now empty
    r = await reader.call_tool("receive", {"channel": "general"})
    assert r["count"] == 0


# ------------------------------------------------------------------
# Send-to-self
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_sends_to_own_channel_and_reads(test_db, mcp_client):
    """A peer can send to a channel and read their own messages."""
    tok = await seed_peer(test_db, "alice")
    client = mcp_client(tok, "alice")
    await client.initialize()

    await client.call_tool("send", {"channel": "notes", "content": "my first note"})
    await client.call_tool("send", {"channel": "notes", "content": "my second note"})

    r = await client.call_tool("receive", {"channel": "notes"})
    assert r["ok"] is True
    assert r["count"] == 2
    assert r["messages"][0]["from"] == "alice"


# ------------------------------------------------------------------
# Empty channels and peers
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonexistent_channel_receive(test_db, mcp_client):
    """Receiving from a channel that doesn't exist returns an error."""
    tok = await seed_peer(test_db, "alice")
    client = mcp_client(tok, "alice")
    await client.initialize()

    r = await client.call_tool("receive", {"channel": "does-not-exist"})
    assert r["ok"] is False
    assert "does not exist" in r["error"]
    assert "list_channels" in r["hint"]


@pytest.mark.asyncio
async def test_heartbeat_no_unread_returns_empty(test_db, mcp_client):
    """Heartbeat with no unread messages returns an empty unread_summary."""
    tok = await seed_peer(test_db, "alice")
    client = mcp_client(tok, "alice")
    await client.initialize()

    r = await client.call_tool("heartbeat", {})
    assert r["ok"] is True
    assert r["unread_summary"]["total_unread"] == 0
    assert r["unread_summary"]["channels"] == []
    assert r["peers_online"] == 0  # alice excludes herself from the list


@pytest.mark.asyncio
async def test_list_peers_empty_namespace(test_db, mcp_client):
    """list_peers in a namespace with only the caller should return just herself."""
    tok = await seed_peer(test_db, "loner", "solo-ns")
    client = mcp_client(tok, "loner")
    await client.initialize()

    r = await client.call_tool("list_peers", {})
    assert r["ok"] is True
    # Only the caller's own peer row after auth middleware upserts it
    assert r["total"] >= 1
    assert any(p["name"] == "loner" for p in r["peers"])


# ------------------------------------------------------------------
# Newlines and special characters in content
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiline_content(test_db, mcp_client):
    """Multi-line messages should roundtrip."""
    sender_tok = await seed_peer(test_db, "sender")
    reader_tok = await seed_peer(test_db, "reader")
    sender = mcp_client(sender_tok, "sender")
    reader = mcp_client(reader_tok, "reader")
    await sender.initialize()
    await reader.initialize()

    content = "Line 1\nLine 2\nLine 3\n\nLine 5 after blank\n\tIndented\n"
    r = await sender.call_tool("send", {"channel": "general", "content": content})
    assert r["ok"] is True

    r = await reader.call_tool("receive", {"channel": "general"})
    assert r["messages"][0]["content"] == content


@pytest.mark.asyncio
async def test_code_snippet_content(test_db, mcp_client):
    """Code with quotes, backslashes, and special chars should roundtrip."""
    sender_tok = await seed_peer(test_db, "sender")
    reader_tok = await seed_peer(test_db, "reader")
    sender = mcp_client(sender_tok, "sender")
    reader = mcp_client(reader_tok, "reader")
    await sender.initialize()
    await reader.initialize()

    code = '''def hello(name: str) -> str:
    """Return a greeting."""
    return f"Hello, {name}!\\n"  # escape sequence
'''
    r = await sender.call_tool("send", {"channel": "general", "content": code})
    assert r["ok"] is True

    r = await reader.call_tool("receive", {"channel": "general"})
    assert r["messages"][0]["content"] == code


# ------------------------------------------------------------------
# Dashboard API edge cases
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_api_empty_state(test_db, http_client):
    """Dashboard API with no data returns empty lists for the caller's namespace."""
    tok = await seed_peer(test_db, "alice")
    resp = await http_client.get(
        "/dashboard/api", headers={"Authorization": f"Bearer {tok}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["namespace"] == "default"
    assert data["channels"] == []
    assert data["messages"] == []
    # Dashboard bypasses the MCP middleware, so no automatic peer upsert.
    # The peers list may be empty if alice hasn't heartbeated yet.
    assert isinstance(data["peers"], list)


@pytest.mark.asyncio
async def test_dashboard_api_reflects_new_messages(test_db, mcp_client, http_client):
    """Dashboard API should reflect messages sent via MCP tools."""
    tok = await seed_peer(test_db, "alice")
    client = mcp_client(tok, "alice")
    await client.initialize()

    await client.call_tool("send", {"channel": "general", "content": "hello dashboard"})

    resp = await http_client.get(
        "/dashboard/api", headers={"Authorization": f"Bearer {tok}"}
    )
    data = resp.json()
    assert len(data["messages"]) == 1
    assert data["messages"][0]["content"] == "hello dashboard"
    assert data["messages"][0]["sender"] == "alice"
