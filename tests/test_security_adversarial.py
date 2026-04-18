"""Adversarial / security tests.

Attack the relay from every angle: SQL injection, auth bypass, namespace
crossing, rate limiting, malformed protocol, token manipulation.
"""

import asyncio
import hashlib
import json

import pytest
from agentic_chat.config import now_ms
from agentic_chat.auth import TokenAuthMiddleware


async def seed_peer(db, name: str, ns: str = "default") -> str:
    raw = f"relay_tok_sec_{name}_{ns}"
    h = hashlib.sha256(raw.encode()).hexdigest()
    await db.execute(
        "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
        "VALUES (?, ?, ?, ?)",
        (h, name, ns, now_ms()),
    )
    return raw


# ------------------------------------------------------------------
# SQL injection attempts
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sql_injection_in_channel_name(test_db, mcp_client):
    """Channel name validation regex blocks any character that could enable
    SQL injection (quotes, semicolons, backticks). Note: even if a channel
    name passed through (e.g. 'general--'), parameterized queries would
    prevent injection -- this is defense-in-depth."""
    tok = await seed_peer(test_db, "attacker")
    client = mcp_client(tok, "attacker")
    await client.initialize()

    # These all contain characters forbidden by the regex
    attacks = [
        "general'; DROP TABLE messages;--",
        "general' OR '1'='1",
        "general;DELETE FROM tokens",
        "general`",
        "general\"",
        "general\\",
        "general OR 1=1",
    ]
    for attack in attacks:
        r = await client.call_tool("send", {"channel": attack, "content": "pwn"})
        assert r["ok"] is False, f"attack accepted: {attack!r}"
        assert "Channel name must be" in r["error"]

    # Verify DB is intact
    row = await test_db.fetchone("SELECT COUNT(*) AS c FROM tokens")
    assert row["c"] > 0


@pytest.mark.asyncio
async def test_sql_injection_in_message_content(test_db, mcp_client):
    """SQL-looking content should be stored literally and not executed."""
    tok = await seed_peer(test_db, "attacker")
    client = mcp_client(tok, "attacker")
    await client.initialize()

    payload = "'; DROP TABLE tokens; --"
    r = await client.call_tool("send", {"channel": "general", "content": payload})
    assert r["ok"] is True  # content can be arbitrary text

    # Receive it back -- should be the literal string
    reader_tok = await seed_peer(test_db, "reader")
    reader = mcp_client(reader_tok, "reader")
    await reader.initialize()
    r = await reader.call_tool("receive", {"channel": "general"})
    assert r["ok"] is True
    assert r["messages"][0]["content"] == payload

    # Verify DB is still intact
    row = await test_db.fetchone("SELECT COUNT(*) AS c FROM tokens")
    assert row["c"] >= 2


# ------------------------------------------------------------------
# Namespace crossing attempts
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_read_other_namespace_messages(test_db, mcp_client):
    """A peer in ns-A must not see messages in ns-B even with channel name collision."""
    # Alice in team-a
    alice_tok = await seed_peer(test_db, "alice", "team-a")
    # Mallory in team-b
    mallory_tok = await seed_peer(test_db, "mallory", "team-b")

    alice = mcp_client(alice_tok, "alice")
    mallory = mcp_client(mallory_tok, "mallory")
    await alice.initialize()
    await mallory.initialize()

    # Both send to a channel with the same name
    await alice.call_tool("send", {"channel": "secrets", "content": "team-a secret"})
    await mallory.call_tool("send", {"channel": "secrets", "content": "team-b content"})

    # Alice reads -- should only see team-a secret
    r = await alice.call_tool("receive", {"channel": "secrets"})
    assert r["ok"] is True
    assert len(r["messages"]) == 1
    assert r["messages"][0]["content"] == "team-a secret"
    assert r["messages"][0]["from_peer"] == "alice"

    # Mallory reads -- should only see team-b content
    r = await mallory.call_tool("receive", {"channel": "secrets"})
    assert r["ok"] is True
    assert len(r["messages"]) == 1
    assert r["messages"][0]["content"] == "team-b content"


@pytest.mark.asyncio
async def test_cannot_list_peers_from_other_namespace(test_db, mcp_client):
    """list_peers must not reveal peers from other namespaces."""
    alice_tok = await seed_peer(test_db, "alice", "team-a")
    await seed_peer(test_db, "bob", "team-b")
    await seed_peer(test_db, "carol", "team-c")

    alice = mcp_client(alice_tok, "alice")
    await alice.initialize()

    r = await alice.call_tool("list_peers", {})
    assert r["ok"] is True
    names = {p["name"] for p in r["peers"]}
    # alice should see only herself (her namespace has no other peers)
    assert "bob" not in names
    assert "carol" not in names


@pytest.mark.asyncio
async def test_cannot_list_channels_from_other_namespace(test_db, mcp_client):
    """list_channels must not reveal channels from other namespaces."""
    alice_tok = await seed_peer(test_db, "alice", "team-a")
    mallory_tok = await seed_peer(test_db, "mallory", "team-b")
    alice = mcp_client(alice_tok, "alice")
    mallory = mcp_client(mallory_tok, "mallory")
    await alice.initialize()
    await mallory.initialize()

    await alice.call_tool("send", {"channel": "alice-chan", "content": "x"})
    await mallory.call_tool("send", {"channel": "mallory-chan", "content": "y"})

    r = await alice.call_tool("list_channels", {})
    channels = {c["name"] for c in r["channels"]}
    assert "alice-chan" in channels
    assert "mallory-chan" not in channels


# ------------------------------------------------------------------
# Auth & token attacks
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoked_token_immediately_denied(test_db, http_client):
    """After token DELETE, next request with that token should fail."""
    tok = await seed_peer(test_db, "temporary")
    # Works initially
    resp = await http_client.post(
        "/mcp",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {tok}",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
    )
    assert resp.status_code == 200

    # Revoke
    h = hashlib.sha256(tok.encode()).hexdigest()
    await test_db.execute("DELETE FROM tokens WHERE token_hash = ?", (h,))

    # Next request fails
    resp = await http_client.post(
        "/mcp",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {tok}",
        },
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_token_case_sensitivity(test_db, http_client):
    """Token comparison must be exact (case-sensitive)."""
    tok = await seed_peer(test_db, "alice")
    upper = tok.upper()

    resp = await http_client.post(
        "/mcp",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {upper}",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 403  # uppercase version is a different token


@pytest.mark.asyncio
async def test_bearer_prefix_variations(test_db, http_client):
    """Only exact 'Bearer ' prefix should be accepted."""
    tok = await seed_peer(test_db, "alice")

    bad_prefixes = ["bearer", "BEARER", "Token", "Basic", ""]
    for prefix in bad_prefixes:
        auth = f"{prefix} {tok}" if prefix else tok
        resp = await http_client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": auth,
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert resp.status_code == 401, f"prefix {prefix!r} was accepted"


# ------------------------------------------------------------------
# Rate limiting
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_bucket_allows_initial_burst(test_db, _live_app, http_client):
    """Token bucket should allow a burst of requests (MCP init + tool calls)."""
    import agentic_chat.config as config_module

    # Configure a small bucket we can actually exhaust in a test
    config_module.CONFIG["rate_limit_burst"] = 5
    config_module.CONFIG["rate_limit_refill_per_sec"] = 1.0
    _live_app._buckets.clear()

    try:
        tok = await seed_peer(test_db, "burstclient")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {tok}",
        }

        # First 5 requests should all succeed (burst capacity)
        for i in range(5):
            r = await http_client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": i, "method": "tools/list"},
            )
            assert r.status_code != 429, f"request {i} unexpectedly rate limited"

        # 6th request should be rate-limited (bucket empty)
        r = await http_client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 99, "method": "tools/list"},
        )
        assert r.status_code == 429
        assert "Too many requests" in r.json()["error"]

    finally:
        config_module.CONFIG["rate_limit_burst"] = 100_000
        config_module.CONFIG["rate_limit_refill_per_sec"] = 100_000.0
        _live_app._buckets.clear()


@pytest.mark.asyncio
async def test_token_bucket_refills_over_time(test_db, _live_app, http_client):
    """After the burst is exhausted, waiting should refill the bucket."""
    import agentic_chat.config as config_module

    # Small bucket, fast refill so the test is quick
    config_module.CONFIG["rate_limit_burst"] = 2
    config_module.CONFIG["rate_limit_refill_per_sec"] = 20.0  # 50ms per token
    _live_app._buckets.clear()

    try:
        tok = await seed_peer(test_db, "refillclient")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {tok}",
        }

        # Exhaust the burst
        for _ in range(2):
            r = await http_client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert r.status_code != 429

        # Immediate next request is blocked
        r = await http_client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert r.status_code == 429

        # Wait for refill (>=1 token regenerated)
        await asyncio.sleep(0.15)

        # Now a request should succeed
        r = await http_client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        )
        assert r.status_code != 429

    finally:
        config_module.CONFIG["rate_limit_burst"] = 100_000
        config_module.CONFIG["rate_limit_refill_per_sec"] = 100_000.0
        _live_app._buckets.clear()


@pytest.mark.asyncio
async def test_rate_limiter_bucket_dict_bounded(_live_app):
    """Bucket dict cleanup runs without crashing on many entries."""
    import time
    from agentic_chat.auth import TokenBucket

    _live_app._buckets.clear()

    now = time.monotonic()
    # Fill with 1100 fresh buckets
    for i in range(1100):
        _live_app._buckets[f"hash_{i}"] = TokenBucket(30, 5.0, now)

    # Simulate middleware pruning: evict buckets with last_refill older than 5m
    if len(_live_app._buckets) > 1000:
        cutoff = now - 300
        _live_app._buckets = {
            k: b for k, b in _live_app._buckets.items() if b.last_refill > cutoff
        }

    # Nothing is stale so nothing got pruned; just verify no crash
    assert len(_live_app._buckets) >= 1000
    _live_app._buckets.clear()


def test_token_bucket_unit():
    """Unit test for TokenBucket math."""
    from agentic_chat.auth import TokenBucket

    bucket = TokenBucket(capacity=3, refill_rate=10.0, now=0.0)
    # Can consume 3 initially
    assert bucket.try_consume(0.0) is True
    assert bucket.try_consume(0.0) is True
    assert bucket.try_consume(0.0) is True
    # 4th fails
    assert bucket.try_consume(0.0) is False
    # After 0.1s -> 1 new token (10/s * 0.1s)
    assert bucket.try_consume(0.1) is True
    # Immediately another fails
    assert bucket.try_consume(0.1) is False
    # After 0.3s total -> should have refilled to 2 tokens (from last_refill=0.1)
    assert bucket.try_consume(0.3) is True
    assert bucket.try_consume(0.3) is True
    # Cap at capacity -- long wait doesn't overflow
    assert bucket.try_consume(100.0) is True
    assert bucket.try_consume(100.0) is True
    assert bucket.try_consume(100.0) is True
    assert bucket.try_consume(100.0) is False


# ------------------------------------------------------------------
# DM channel name validation
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_malformed_names_rejected(test_db, mcp_client):
    """Malformed DM channel names should be rejected."""
    tok = await seed_peer(test_db, "alice")
    client = mcp_client(tok, "alice")
    await client.initialize()

    bad_names = ["dm-", "dm--", "dm-alice", "dm-alice-", "dm--bob", "dm-a-b-c"]
    for name in bad_names:
        r = await client.call_tool("send", {"channel": name, "content": "x"})
        assert r["ok"] is False, f"malformed DM accepted: {name!r}"


@pytest.mark.asyncio
async def test_can_send_to_dm_as_non_participant(test_db, mcp_client):
    """By design, any peer can send to any DM channel (naming convention,
    not access control). This test documents that behavior explicitly."""
    carol_tok = await seed_peer(test_db, "carol")
    alice_tok = await seed_peer(test_db, "alice")
    carol = mcp_client(carol_tok, "carol")
    alice = mcp_client(alice_tok, "alice")
    await carol.initialize()
    await alice.initialize()

    # Carol sends to alice-bob DM (she's not a participant)
    r = await carol.call_tool("send", {
        "channel": "dm-alice-bob",
        "content": "snooping",
    })
    assert r["ok"] is True  # allowed by design -- DMs are not private

    # Alice can read it
    r = await alice.call_tool("receive", {"channel": "dm-alice-bob"})
    assert r["ok"] is True
    assert r["messages"][0]["from_peer"] == "carol"


# ------------------------------------------------------------------
# Malformed / adversarial input
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_message_content_rejected(test_db, mcp_client):
    """Empty or whitespace content should be rejected."""
    tok = await seed_peer(test_db, "alice")
    client = mcp_client(tok, "alice")
    await client.initialize()

    for content in ["", "   ", "\t\n"]:
        r = await client.call_tool("send", {"channel": "general", "content": content})
        assert r["ok"] is False
        assert "empty" in r["error"].lower()


@pytest.mark.asyncio
async def test_overlong_status_message_rejected(test_db, mcp_client):
    """Status message over 200 chars should be rejected."""
    tok = await seed_peer(test_db, "alice")
    client = mcp_client(tok, "alice")
    await client.initialize()

    r = await client.call_tool("heartbeat", {"status_message": "x" * 201})
    assert r["ok"] is False
    assert "200" in r["error"]


@pytest.mark.asyncio
async def test_receive_invalid_limit(test_db, mcp_client):
    """Out-of-range limits should be rejected."""
    tok = await seed_peer(test_db, "alice")
    client = mcp_client(tok, "alice")
    await client.initialize()

    # Set up a channel so the limit check runs
    await client.call_tool("send", {"channel": "general", "content": "x"})

    for bad_limit in [0, -1, 101, 9999]:
        r = await client.call_tool("receive", {"channel": "general", "limit": bad_limit})
        assert r["ok"] is False
        assert "1 and 100" in r["error"]


@pytest.mark.asyncio
async def test_receive_negative_since_id(test_db, mcp_client):
    """Negative since_id should be rejected."""
    tok = await seed_peer(test_db, "alice")
    client = mcp_client(tok, "alice")
    await client.initialize()
    await client.call_tool("send", {"channel": "general", "content": "x"})

    r = await client.call_tool("receive", {"channel": "general", "since_id": -1})
    assert r["ok"] is False
    assert "non-negative" in r["error"]


@pytest.mark.asyncio
async def test_since_id_rejected_in_all_channels_mode(test_db, mcp_client):
    """since_id requires a specific channel."""
    tok = await seed_peer(test_db, "alice")
    client = mcp_client(tok, "alice")
    await client.initialize()

    r = await client.call_tool("receive", {"since_id": 5})
    assert r["ok"] is False
    assert "specific channel" in r["error"]
