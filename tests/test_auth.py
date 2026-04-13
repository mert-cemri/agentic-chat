"""Tests for authentication middleware and token management."""

import hashlib
import time

import pytest
from agentic_chat.config import now_ms
from agentic_chat.channels import normalize_channel, PEER_NAME_RE, CHANNEL_NAME_RE


# -- Token hashing tests --


def test_token_hashing():
    """Raw token should produce consistent sha256 hash."""
    raw = "relay_tok_test_alice"
    h = hashlib.sha256(raw.encode()).hexdigest()
    assert len(h) == 64
    assert h == hashlib.sha256(raw.encode()).hexdigest()  # deterministic


@pytest.mark.asyncio
async def test_token_lookup_by_hash(seeded_db):
    """Token lookup should work by hash, returning peer_name and namespace."""
    db, tokens = seeded_db
    raw = tokens["alice"]
    h = hashlib.sha256(raw.encode()).hexdigest()
    row = await db.fetchone(
        "SELECT peer_name, namespace FROM tokens WHERE token_hash = ?", (h,)
    )
    assert row is not None
    assert row["peer_name"] == "alice"
    assert row["namespace"] == "default"


@pytest.mark.asyncio
async def test_invalid_token_returns_none(seeded_db):
    """Invalid token hash should return None."""
    db, _ = seeded_db
    h = hashlib.sha256(b"nonexistent_token").hexdigest()
    row = await db.fetchone(
        "SELECT peer_name, namespace FROM tokens WHERE token_hash = ?", (h,)
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
        "DELETE FROM tokens WHERE peer_name = ? AND namespace = ?",
        ("alice", "default"),
    )

    # Verify gone
    row = await db.fetchone("SELECT * FROM tokens WHERE token_hash = ?", (h,))
    assert row is None


@pytest.mark.asyncio
async def test_token_recreate_after_revoke(seeded_db):
    """After revoking, creating a new token for the same peer should work."""
    db, tokens = seeded_db

    await db.execute(
        "DELETE FROM tokens WHERE peer_name = ? AND namespace = ?",
        ("alice", "default"),
    )

    new_hash = hashlib.sha256(b"relay_tok_new_alice").hexdigest()
    await db.execute(
        "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
        "VALUES (?, 'alice', 'default', ?)",
        (new_hash, now_ms()),
    )

    row = await db.fetchone(
        "SELECT peer_name FROM tokens WHERE token_hash = ?", (new_hash,)
    )
    assert row["peer_name"] == "alice"


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


def test_normalize_dm_three_parts():
    """dm-a-b-c should error (too many parts)."""
    _, err = normalize_channel("dm-a-b-c")
    assert err is not None


def test_normalize_dm_one_part():
    """dm-alice (only one name) should error."""
    _, err = normalize_channel("dm-alice")
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
