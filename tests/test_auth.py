"""Tests for authentication middleware and token management."""

import hashlib
import time

import pytest
from agentic_chat.config import now_ms
from agentic_chat.channels import (
    normalize_channel,
    PEER_NAME_RE,
    CHANNEL_NAME_RE,
    OWNER_NAME_RE,
    validate_session_peer_name,
)
from agentic_chat.auth import resolve_peer_name


# -- Token hashing tests --


def test_token_hashing():
    """Raw token should produce consistent sha256 hash."""
    raw = "relay_tok_test_alice"
    h = hashlib.sha256(raw.encode()).hexdigest()
    assert len(h) == 64
    assert h == hashlib.sha256(raw.encode()).hexdigest()  # deterministic


@pytest.mark.asyncio
async def test_token_lookup_by_hash(seeded_db):
    """Token lookup should work by hash, returning owner_name and namespace."""
    db, tokens = seeded_db
    raw = tokens["alice"]
    h = hashlib.sha256(raw.encode()).hexdigest()
    row = await db.fetchone(
        "SELECT owner_name, namespace FROM tokens WHERE token_hash = ?", (h,)
    )
    assert row is not None
    assert row["owner_name"] == "alice"
    assert row["namespace"] == "default"


@pytest.mark.asyncio
async def test_invalid_token_returns_none(seeded_db):
    """Invalid token hash should return None."""
    db, _ = seeded_db
    h = hashlib.sha256(b"nonexistent_token").hexdigest()
    row = await db.fetchone(
        "SELECT owner_name, namespace FROM tokens WHERE token_hash = ?", (h,)
    )
    assert row is None


@pytest.mark.asyncio
async def test_token_revocation_deletes_row(seeded_db):
    """Revoking a token should DELETE the row, not soft-delete."""
    db, tokens = seeded_db
    h = hashlib.sha256(tokens["alice"].encode()).hexdigest()

    # Verify exists
    row = await db.fetchone("SELECT * FROM tokens WHERE token_hash = ?", (h,))
    assert row is not None

    # Revoke
    await db.execute(
        "DELETE FROM tokens WHERE owner_name = ? AND namespace = ?",
        ("alice", "default"),
    )

    # Verify gone
    row = await db.fetchone("SELECT * FROM tokens WHERE token_hash = ?", (h,))
    assert row is None


@pytest.mark.asyncio
async def test_token_recreate_after_revoke(seeded_db):
    """After revoking, creating a new token for the same owner should work."""
    db, tokens = seeded_db

    await db.execute(
        "DELETE FROM tokens WHERE owner_name = ? AND namespace = ?",
        ("alice", "default"),
    )

    new_hash = hashlib.sha256(b"relay_tok_new_alice").hexdigest()
    await db.execute(
        "INSERT INTO tokens (token_hash, owner_name, namespace, created_at) "
        "VALUES (?, 'alice', 'default', ?)",
        (new_hash, now_ms()),
    )

    row = await db.fetchone(
        "SELECT owner_name FROM tokens WHERE token_hash = ?", (new_hash,)
    )
    assert row["owner_name"] == "alice"


# -- normalize_channel tests --


def test_normalize_non_dm():
    """Non-DM channels should pass through unchanged."""
    assert normalize_channel("general") == ("general", None)
    assert normalize_channel("frontend-team") == ("frontend-team", None)


def test_normalize_dm_sorts_alphabetically():
    """DM names should be sorted alphabetically."""
    assert normalize_channel("dm-bob-alice") == ("dm-alice-bob", None)
    assert normalize_channel("dm-alice-bob") == ("dm-alice-bob", None)


def test_normalize_dm_lowercases():
    """DM names should be lowercased."""
    assert normalize_channel("dm-Alice-Bob") == ("dm-alice-bob", None)
    assert normalize_channel("dm-ZARA-alice") == ("dm-alice-zara", None)


def test_normalize_dm_prefix_case_insensitive():
    """DM prefix detection must be case-insensitive.
    `DM-Alice-Bob`, `Dm-alice-bob`, etc. should all normalize to `dm-alice-bob`."""
    assert normalize_channel("DM-Alice-Bob") == ("dm-alice-bob", None)
    assert normalize_channel("Dm-alice-bob") == ("dm-alice-bob", None)
    assert normalize_channel("dM-ZARA-ALICE") == ("dm-alice-zara", None)


def test_normalize_dm_empty_prefix():
    """dm- with no names should error."""
    _, err = normalize_channel("dm-")
    assert err is not None


def test_normalize_dm_empty_parts():
    """dm-- (empty names) should error."""
    _, err = normalize_channel("dm--")
    assert err is not None

    _, err = normalize_channel("dm-alice-")
    assert err is not None

    _, err = normalize_channel("dm--bob")
    assert err is not None


def test_normalize_dm_three_parts_suggests_group():
    """dm-a-b-c should error and suggest group- prefix."""
    _, err = normalize_channel("dm-a-b-c")
    assert err is not None
    assert "group-" in err  # should suggest using group channel


def test_normalize_dm_one_part():
    """dm-alice (only one name) should error."""
    _, err = normalize_channel("dm-alice")
    assert err is not None


# -- Group channel tests --


def test_normalize_group_sorts_and_dedupes():
    """Group channel names should be sorted and deduplicated."""
    assert normalize_channel("group-carol-bob-alice") == ("group-alice-bob-carol", None)
    assert normalize_channel("group-bob-alice-bob") == ("group-alice-bob", None)  # dedup


def test_normalize_group_lowercases():
    """Group names should be lowercased."""
    assert normalize_channel("Group-Alice-Bob-Carol") == ("group-alice-bob-carol", None)
    assert normalize_channel("GROUP-ZAR-ALI") == ("group-ali-zar", None)


def test_normalize_group_needs_2_plus():
    """Group with fewer than 2 names should error."""
    _, err = normalize_channel("group-alice")
    assert err is not None
    _, err = normalize_channel("group-")
    assert err is not None


# -- Self channel tests --


def test_normalize_self_with_name():
    """Self channel normalizes to lowercase."""
    assert normalize_channel("self-Alice") == ("self-alice", None)
    assert normalize_channel("SELF-MERT") == ("self-mert", None)


def test_normalize_self_with_caller():
    """Self channel without explicit name uses caller_name."""
    assert normalize_channel("self-", caller_name="mert") == ("self-mert", None)


def test_normalize_self_no_name_no_caller():
    """Self channel without name or caller should error."""
    _, err = normalize_channel("self-")
    assert err is not None


# -- Regex validation tests --


def test_peer_name_regex():
    """Peer name regex should accept valid names and reject invalid ones."""
    assert PEER_NAME_RE.match("alice")
    assert PEER_NAME_RE.match("alice_dev")
    assert PEER_NAME_RE.match("alice-dev")
    assert PEER_NAME_RE.match("a")
    assert PEER_NAME_RE.match("A1_b-c")
    assert not PEER_NAME_RE.match("")
    assert not PEER_NAME_RE.match("-alice")
    assert not PEER_NAME_RE.match("_alice")
    assert not PEER_NAME_RE.match("al!ce")
    assert not PEER_NAME_RE.match("a" * 33)  # too long


def test_channel_name_regex():
    """Channel name regex should accept valid names and reject invalid ones."""
    assert CHANNEL_NAME_RE.match("general")
    assert CHANNEL_NAME_RE.match("dm-alice-bob")
    assert CHANNEL_NAME_RE.match("frontend-team")
    assert CHANNEL_NAME_RE.match("a")
    assert not CHANNEL_NAME_RE.match("")
    assert not CHANNEL_NAME_RE.match("-general")
    assert not CHANNEL_NAME_RE.match("gen eral")
    assert not CHANNEL_NAME_RE.match("a" * 65)  # too long


# -- Owner/session-peer validation tests --


def test_owner_name_regex_rejects_hyphens():
    """Owner names cannot contain hyphens (hyphens separate owner from suffix)."""
    assert OWNER_NAME_RE.match("alice")
    assert OWNER_NAME_RE.match("alice_dev")
    assert OWNER_NAME_RE.match("A1_b")
    assert not OWNER_NAME_RE.match("alice-dev")  # would shadow sub-owners
    assert not OWNER_NAME_RE.match("")
    assert not OWNER_NAME_RE.match("_alice")
    assert not OWNER_NAME_RE.match("a" * 32)  # one over the 31-char limit


def test_validate_session_peer_allows_owner_exactly():
    assert validate_session_peer_name("alice", "alice") is None


def test_validate_session_peer_allows_owner_dash_suffix():
    assert validate_session_peer_name("alice-laptop", "alice") is None
    assert validate_session_peer_name("alice-desktop_2", "alice") is None


def test_validate_session_peer_rejects_other_owner():
    err = validate_session_peer_name("bob", "alice")
    assert err is not None and "not permitted" in err


def test_validate_session_peer_rejects_empty_suffix():
    # "alice-" has the prefix but an empty suffix -- also fails PEER_NAME_RE.
    err = validate_session_peer_name("alice-", "alice")
    assert err is not None


def test_validate_session_peer_rejects_invalid_chars():
    # Spaces, punctuation, etc. are rejected by PEER_NAME_RE before we get to
    # the owner-prefix check.
    err = validate_session_peer_name("alice laptop", "alice")
    assert err is not None


def test_validate_session_peer_rejects_close_but_wrong_prefix():
    # "alicex-laptop" starts with "alice" but not "alice-" -- must not match.
    err = validate_session_peer_name("alicex-laptop", "alice")
    assert err is not None


def test_resolve_peer_name_defaults_to_owner():
    """Clients that don't send X-Peer-Name keep the pre-refactor behaviour."""
    name, err = resolve_peer_name("alice", None)
    assert err is None
    assert name == "alice"
    name, err = resolve_peer_name("alice", "")
    assert err is None
    assert name == "alice"


def test_resolve_peer_name_accepts_valid_declared():
    name, err = resolve_peer_name("alice", "alice-laptop")
    assert err is None
    assert name == "alice-laptop"


def test_resolve_peer_name_rejects_invalid_declared():
    _, err = resolve_peer_name("alice", "bob")
    assert err is not None


# -- End-to-end X-Peer-Name header tests --


@pytest.mark.asyncio
async def test_http_header_picks_session_name(seeded_db, mcp_client):
    """Heartbeat routed through the full ASGI stack should honour X-Peer-Name."""
    _, tokens = seeded_db
    client = mcp_client(tokens["alice"], "alice")
    # Override the header on the underlying http client for this session
    client.http.headers["X-Peer-Name"] = "alice-laptop"
    await client.initialize()
    result = await client.call_tool("heartbeat", {})
    assert result["ok"] is True
    assert result["you"]["peer_name"] == "alice-laptop"
    # Clean up for other tests that share this http client
    client.http.headers.pop("X-Peer-Name", None)


@pytest.mark.asyncio
async def test_http_header_rejects_foreign_name(seeded_db, mcp_client, http_client):
    """A token for `alice` must not be usable as peer_name=bob."""
    _, tokens = seeded_db
    # Go one level below MCPClient so we can inspect the raw 403.
    resp = await http_client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {tokens['alice']}",
            "X-Peer-Name": "bob",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1.0"},
            },
        },
    )
    assert resp.status_code == 403
    assert "not permitted" in resp.json()["error"]


@pytest.mark.asyncio
async def test_http_absent_header_falls_back_to_owner(seeded_db, mcp_client):
    """No X-Peer-Name header -> peer_name == owner_name (back-compat)."""
    _, tokens = seeded_db
    client = mcp_client(tokens["alice"], "alice")
    await client.initialize()
    result = await client.call_tool("heartbeat", {})
    assert result["ok"] is True
    assert result["you"]["peer_name"] == "alice"


@pytest.mark.asyncio
async def test_two_sessions_one_owner_distinct_peers(seeded_db, http_client):
    """Two sessions sharing a token but with different X-Peer-Name values
    show up as two distinct peers in the peers table."""
    import json as _json
    _, tokens = seeded_db
    token = tokens["alice"]

    for suffix in ("laptop", "desktop"):
        resp = await http_client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Peer-Name": f"alice-{suffix}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "s", "version": "1.0"},
                },
            },
        )
        assert resp.status_code == 200, resp.text

    # The middleware upserts peers on auth; both session names should exist.
    db, _ = seeded_db
    rows = await db.fetchall(
        "SELECT peer_name FROM peers WHERE namespace='default' AND peer_name LIKE 'alice-%' ORDER BY peer_name"
    )
    names = [r["peer_name"] for r in rows]
    assert names == ["alice-desktop", "alice-laptop"]
