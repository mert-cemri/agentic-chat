#!/usr/bin/env python3
"""
claude-relay: A message relay server for Claude Code instances.
Single-file implementation: server + CLI.

Usage:
    python relay.py init                    # First-time setup
    python relay.py serve                   # Start the server
    python relay.py token create --name X   # Create a peer token
    python relay.py token list              # List tokens
    python relay.py token revoke --name X   # Revoke a token
    python relay.py check                   # Verify deployment
"""

# -- Imports -------------------------------------------------------

import argparse
import asyncio
import hashlib
import json
import logging
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from mcp.server.fastmcp import FastMCP, Context
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# -- Logging -------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("relay")

# -- Configuration -------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "port": 4444,
    "host": "0.0.0.0",
    "db_path": "./data/relay.db",
    "heartbeat_timeout_seconds": 120,
    "message_retention_days": 7,
    "max_message_length": 50000,
    "cleanup_batch_size": 5000,
    "max_receive_response_bytes": 102400,  # 100KB
    # Token bucket rate limiter: burst of N requests, refilled at R/s.
    # Default allows 30-request bursts (covers MCP init + tool calls) and
    # sustains 5 req/s per authenticated token.
    "rate_limit_burst": 30,
    "rate_limit_refill_per_sec": 5.0,
    # Public URL used for generating join links. If null, the request's
    # Host header is used (convenient for localhost dev, but vulnerable
    # to header poisoning on public deployments — set explicitly).
    "public_url": None,
}

CONFIG: dict[str, Any] = {}


def load_config() -> dict[str, Any]:
    """Load config from relay.config.json, falling back to defaults."""
    config = dict(DEFAULT_CONFIG)
    config_path = Path("relay.config.json")
    if config_path.exists():
        with open(config_path) as f:
            overrides = json.load(f)
        config.update(overrides)
        log.info("Loaded config from %s", config_path)
    else:
        log.info("No config file found, using defaults")
    return config


def validate_config(config: dict[str, Any]) -> None:
    """Validate config types and ranges. Raises ValueError on invalid config."""
    int_checks = {
        "port": (int, 1, 65535),
        "heartbeat_timeout_seconds": (int, 10, 3600),
        "message_retention_days": (int, 1, 365),
        "max_message_length": (int, 100, 1_000_000),
        "cleanup_batch_size": (int, 100, 100_000),
        "max_receive_response_bytes": (int, 1024, 10_000_000),
        "rate_limit_burst": (int, 1, 10_000),
    }
    for key, (expected_type, min_val, max_val) in int_checks.items():
        val = config.get(key)
        if val is None:
            raise ValueError(f"Missing config key: {key}")
        if not isinstance(val, expected_type):
            raise ValueError(
                f"Config '{key}' must be {expected_type.__name__}, "
                f"got {type(val).__name__}"
            )
        if not (min_val <= val <= max_val):
            raise ValueError(
                f"Config '{key}' must be between {min_val} and {max_val}, got {val}"
            )

    refill = config.get("rate_limit_refill_per_sec")
    if not isinstance(refill, (int, float)) or not (0.1 <= refill <= 1000):
        raise ValueError(
            "Config 'rate_limit_refill_per_sec' must be a number between 0.1 and 1000"
        )

    if not isinstance(config.get("host"), str):
        raise ValueError("Config 'host' must be a string")
    if not isinstance(config.get("db_path"), str):
        raise ValueError("Config 'db_path' must be a string")

    public_url = config.get("public_url")
    if public_url is not None and not isinstance(public_url, str):
        raise ValueError("Config 'public_url' must be a string or null")


def now_ms() -> int:
    """Current time as unix timestamp in milliseconds."""
    return int(time.time() * 1000)


def ms_to_iso(ms: int) -> str:
    """Convert unix ms timestamp to ISO 8601 string."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# -- Database Layer ------------------------------------------------

SCHEMA_SQL = """\
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    peer_name TEXT NOT NULL,
    namespace TEXT NOT NULL DEFAULT 'default',
    created_at INTEGER NOT NULL,
    last_used_at INTEGER
);

CREATE TABLE IF NOT EXISTS peers (
    peer_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'offline',
    status_message TEXT,
    last_heartbeat INTEGER,
    last_heartbeat_monotonic REAL,
    first_seen INTEGER NOT NULL,
    PRIMARY KEY (namespace, peer_name)
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(namespace, name)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    sender_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    content TEXT NOT NULL CHECK(length(content) > 0),
    created_at INTEGER NOT NULL,
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
);

CREATE TABLE IF NOT EXISTS cursors (
    namespace TEXT NOT NULL,
    peer_name TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    last_read_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (namespace, peer_name, channel_id),
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_channel_cursor
    ON messages(channel_id, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_namespace
    ON messages(namespace, created_at);
CREATE INDEX IF NOT EXISTS idx_peers_heartbeat
    ON peers(last_heartbeat);
"""


class RelayDB:
    """Async SQLite wrapper using aiosqlite. Module-level singleton."""

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: str | None = None

    async def connect(self, db_path: str) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        await self._db.execute("UPDATE peers SET status = 'offline'")
        await self._db.commit()
        log.info("Database connected: %s", db_path)

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        assert self._db is not None, "DB not connected"
        cursor = await self._db.execute(sql, params)
        await self._db.commit()
        return cursor

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        assert self._db is not None, "DB not connected"
        cursor = await self._db.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        assert self._db is not None, "DB not connected"
        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def close(self) -> None:
        if self._db:
            try:
                await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            await self._db.close()
            self._db = None
            log.info("Database closed")

    @property
    def path(self) -> str | None:
        return self._db_path


# Module-level singleton -- connected in cmd_serve()
db = RelayDB()

# -- Regex patterns ------------------------------------------------

CHANNEL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,63}$")
PEER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")

# -- Auth Middleware -----------------------------------------------


class TokenBucket:
    """Simple token bucket for burst-friendly rate limiting.

    Allows short bursts up to `capacity` requests, with sustained throughput
    limited to `refill_rate` per second. The bucket refills continuously.
    """

    __slots__ = ("capacity", "refill_rate", "tokens", "last_refill")

    def __init__(self, capacity: int, refill_rate: float, now: float):
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.tokens = float(capacity)
        self.last_refill = now

    def try_consume(self, now: float) -> bool:
        """Attempt to consume one token. Returns True if allowed.

        Uses a small epsilon in the comparison to tolerate floating-point
        drift from the refill computation (e.g., 0.2 * 10.0 = 1.9999...).
        """
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= 1.0 - 1e-9:
            self.tokens = max(0.0, self.tokens - 1.0)
            return True
        return False


class TokenAuthMiddleware:
    """ASGI middleware: validates bearer token, injects peer identity, rate-limits.

    Rate limiting uses a per-token bucket with burst capacity (default 30) and
    sustained refill (default 5/s). This accommodates the burst of requests
    MCP clients fire during initialization while still catching runaway loops.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self._buckets: dict[str, TokenBucket] = {}
        # Defaults; overridden from CONFIG at request time if available.
        self._burst = 30
        self._refill = 5.0

    def _get_bucket(self, token_hash: str, now: float) -> TokenBucket:
        # Pick up config overrides lazily — allows tests and runtime changes
        # to CONFIG without recreating the middleware.
        burst = CONFIG.get("rate_limit_burst", self._burst) if CONFIG else self._burst
        refill = (
            CONFIG.get("rate_limit_refill_per_sec", self._refill)
            if CONFIG else self._refill
        )
        bucket = self._buckets.get(token_hash)
        if bucket is None or bucket.capacity != burst or bucket.refill_rate != refill:
            bucket = TokenBucket(burst, refill, now)
            self._buckets[token_hash] = bucket
        return bucket

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        # /health, /join/, and dashboard routes are public (dashboard API
        # endpoints handle their own auth via _authenticate_dashboard_request).
        if (
            path == "/health"
            or path.startswith("/dashboard")
            or path.startswith("/join/")
        ):
            return await self.app(scope, receive, send)

        # Extract bearer token
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode()

        if not auth.startswith("Bearer "):
            response = JSONResponse(
                {
                    "error": "Missing or invalid Authorization header",
                    "hint": "Include header: Authorization: Bearer <your_token>",
                },
                status_code=401,
            )
            return await response(scope, receive, send)

        raw_token = auth[7:]
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        # Authenticate FIRST (before rate limiting to prevent attacker-controlled dict growth)
        row = await db.fetchone(
            "SELECT peer_name, namespace FROM tokens WHERE token_hash = ?",
            (token_hash,),
        )

        if not row:
            log.warning("Auth failed for token_hash=%s", token_hash[:12])
            response = JSONResponse(
                {
                    "error": "Invalid or revoked token",
                    "hint": "Check your token or ask the relay operator for a new one.",
                },
                status_code=403,
            )
            return await response(scope, receive, send)

        # Token bucket rate limiting (post-auth to prevent attacker dict growth)
        now_mono = time.monotonic()
        bucket = self._get_bucket(token_hash, now_mono)
        if not bucket.try_consume(now_mono):
            log.warning("Rate limited: %s/%s", row["namespace"], row["peer_name"])
            response = JSONResponse(
                {
                    "error": "Too many requests. Please slow down.",
                    "hint": (
                        f"Your token bucket is empty. Sustained rate: "
                        f"{bucket.refill_rate:g}/sec, burst: {int(bucket.capacity)}."
                    ),
                },
                status_code=429,
            )
            return await response(scope, receive, send)

        # Bound dict size to prevent unbounded growth
        if len(self._buckets) > 1000:
            cutoff = now_mono - 300  # evict buckets untouched for > 5 min
            self._buckets = {
                k: b for k, b in self._buckets.items() if b.last_refill > cutoff
            }

        # Inject peer identity into ASGI scope
        scope["relay_peer"] = {
            "peer_name": row["peer_name"],
            "namespace": row["namespace"],
        }

        log.debug("Authenticated: %s/%s", row["namespace"], row["peer_name"])

        # Update last_used_at
        await db.execute(
            "UPDATE tokens SET last_used_at = ? WHERE token_hash = ?",
            (now_ms(), token_hash),
        )

        # Ensure peer row exists. Set `last_heartbeat_monotonic` so the stale
        # peer cleanup actually considers this peer (NULL never compares < cutoff).
        # On re-auth for an existing peer, this is a no-op (ON CONFLICT DO NOTHING).
        await db.execute(
            """INSERT INTO peers (peer_name, namespace, status, last_heartbeat,
               last_heartbeat_monotonic, first_seen)
               VALUES (?, ?, 'online', ?, ?, ?)
               ON CONFLICT(namespace, peer_name) DO NOTHING""",
            (
                row["peer_name"],
                row["namespace"],
                now_ms(),
                time.monotonic(),
                now_ms(),
            ),
        )

        await self.app(scope, receive, send)


# -- Helper: get caller from Context ------------------------------


def get_caller(ctx: Context) -> dict:
    """Extract authenticated peer identity from MCP Context -> ASGI scope."""
    try:
        return ctx.request_context.request.scope["relay_peer"]
    except (AttributeError, KeyError):
        raise RuntimeError("No peer identity in scope -- auth middleware not applied")


# -- Cleanup -------------------------------------------------------

_last_cleanup_mono: float = 0.0


async def maybe_cleanup() -> None:
    """Lazy batched cleanup of expired messages. At most once per hour."""
    global _last_cleanup_mono
    if not CONFIG:
        return
    now_mono = time.monotonic()
    if now_mono - _last_cleanup_mono < 3600:
        return
    _last_cleanup_mono = now_mono

    cutoff = now_ms() - (CONFIG["message_retention_days"] * 86400 * 1000)
    batch_size = CONFIG["cleanup_batch_size"]

    total_deleted = 0
    while True:
        cursor = await db.execute(
            "DELETE FROM messages WHERE rowid IN "
            "(SELECT rowid FROM messages WHERE created_at < ? LIMIT ?)",
            (cutoff, batch_size),
        )
        deleted = cursor.rowcount
        total_deleted += deleted
        if deleted < batch_size:
            break

    if total_deleted > 0:
        log.info("Cleanup: deleted %d expired messages", total_deleted)


# -- DM Normalization ----------------------------------------------


def is_dm_channel(channel: str) -> bool:
    """Check if a channel name is a DM channel (case-insensitive prefix)."""
    return channel.lower().startswith("dm-")


def normalize_channel(channel: str) -> tuple[str, str | None]:
    """Normalize DM channel name ordering. No access control.
    Returns (normalized_name, error_or_None).

    DM format: dm-<name1>-<name2> where names cannot contain hyphens.
    Peers with hyphens in their names should use underscores in DM channels.
    The entire channel name is lowercased (prefix + peer names) so that
    'DM-Alice-Bob' and 'dm-alice-bob' collapse to the same channel.
    """
    if not is_dm_channel(channel):
        return channel, None

    # Strip prefix regardless of its case
    rest = channel[3:]
    if not rest:
        return channel, "DM channel must have exactly two peer names: dm-name1-name2"

    parts = rest.split("-")
    if len(parts) != 2:
        return channel, (
            "DM channel must have exactly two peer names separated by a single hyphen: "
            "dm-name1-name2. Peer names in DMs cannot contain hyphens."
        )

    if not parts[0] or not parts[1]:
        return channel, "DM peer names cannot be empty: dm-name1-name2"

    sorted_parts = sorted(p.lower() for p in parts)
    normalized = f"dm-{sorted_parts[0]}-{sorted_parts[1]}"
    return normalized, None


# -- FastMCP Server ------------------------------------------------

mcp = FastMCP(
    "claude-relay",
    instructions=(
        "You are connected to a Claude Relay server. "
        "This lets you communicate with other Claude Code instances.\n\n"
        "IMPORTANT USAGE PATTERN:\n"
        "1. Call 'heartbeat' first to see who's online and check for unread messages.\n"
        "2. Use 'send' to message a channel. For DMs: send(channel=\"dm-yourname-theirname\"). "
        "The server normalizes the name order.\n"
        "3. Use 'receive' to read messages. Omit 'channel' to get unread from all channels.\n"
        "4. Use 'send(channel=\"general\", ...)' for messages to everyone.\n\n"
        "Your identity is automatically determined from your auth token -- "
        "you do NOT specify who you are.\n"
        "Do NOT call heartbeat repeatedly in a loop. "
        "Only call it when the user asks or at natural breakpoints."
    ),
)


# -- Tool Implementations -----------------------------------------


@mcp.tool()
async def heartbeat(
    status_message: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict:
    """Check in with the relay. Returns who's online and unread message counts.
    Call this when the user asks about the relay or wants to check messages.
    Optionally update your status message (e.g. 'working on auth module')."""
    caller = get_caller(ctx)
    ns = caller["namespace"]
    me = caller["peer_name"]

    try:
        if status_message and len(status_message) > 200:
            return {
                "ok": False,
                "error": "Status message exceeds 200 characters.",
                "hint": "Keep it brief.",
            }

        now = now_ms()
        mono = time.monotonic()

        # Update self
        if status_message is not None:
            await db.execute(
                """UPDATE peers SET status='online', last_heartbeat=?,
                   last_heartbeat_monotonic=?, status_message=?
                   WHERE namespace=? AND peer_name=?""",
                (now, mono, status_message, ns, me),
            )
        else:
            await db.execute(
                """UPDATE peers SET status='online', last_heartbeat=?,
                   last_heartbeat_monotonic=?
                   WHERE namespace=? AND peer_name=?""",
                (now, mono, ns, me),
            )

        # Mark stale peers offline
        timeout = CONFIG.get("heartbeat_timeout_seconds", 120)
        cutoff_mono = mono - timeout
        await db.execute(
            "UPDATE peers SET status='offline' "
            "WHERE namespace=? AND status='online' AND last_heartbeat_monotonic < ?",
            (ns, cutoff_mono),
        )

        # Get peer list
        peers = await db.fetchall(
            """SELECT peer_name, status, status_message, last_heartbeat
               FROM peers WHERE namespace=?
               ORDER BY CASE WHEN status='online' THEN 0 ELSE 1 END, peer_name""",
            (ns,),
        )

        peer_list = []
        peers_online = 0
        for p in peers:
            if p["peer_name"] == me:
                continue
            age = (
                int((now - (p["last_heartbeat"] or 0)) / 1000)
                if p["last_heartbeat"]
                else None
            )
            entry: dict[str, Any] = {
                "name": p["peer_name"],
                "status": p["status"],
                "last_seen_seconds_ago": age,
            }
            if p["status_message"]:
                entry["status_message"] = p["status_message"]
            peer_list.append(entry)
            if p["status"] == "online":
                peers_online += 1

        # Get unread counts
        unread_rows = await db.fetchall(
            """SELECT c.name AS channel, COUNT(m.message_id) AS unread
               FROM channels c
               JOIN messages m ON m.channel_id = c.channel_id
               LEFT JOIN cursors cu ON cu.channel_id = c.channel_id
                   AND cu.namespace = ? AND cu.peer_name = ?
               WHERE c.namespace = ?
                 AND m.message_id > COALESCE(cu.last_read_id, 0)
               GROUP BY c.channel_id HAVING unread > 0""",
            (ns, me, ns),
        )

        total_unread = sum(r["unread"] for r in unread_rows)
        unread_channels = [
            {"channel": r["channel"], "unread": r["unread"]} for r in unread_rows
        ]

        await maybe_cleanup()

        log.info(
            "Heartbeat: %s/%s (online peers: %d, unread: %d)",
            ns,
            me,
            peers_online,
            total_unread,
        )

        return {
            "ok": True,
            "you": {"peer_name": me, "namespace": ns, "status": "online"},
            "peers_online": peers_online,
            "peers": peer_list,
            "unread_summary": {
                "total_unread": total_unread,
                "channels": unread_channels,
            },
        }

    except Exception:
        log.exception("heartbeat error for %s/%s", ns, me)
        return {
            "ok": False,
            "error": "Internal server error.",
            "hint": "Try again in a moment.",
        }


@mcp.tool()
async def send(
    channel: str,
    content: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict:
    """Send a message to a channel. Channel is auto-created if new.
    For DMs, use channel='dm-yourname-theirname' (server normalizes the order).
    For broadcast, use channel='general'."""
    caller = get_caller(ctx)
    ns = caller["namespace"]
    me = caller["peer_name"]

    try:
        if not CHANNEL_NAME_RE.match(channel):
            return {
                "ok": False,
                "error": "Channel name must be 1-64 chars, alphanumeric and hyphens only.",
                "hint": "Example valid names: 'general', 'dm-alice-bob', 'frontend-team'",
            }

        if not content or not content.strip():
            return {
                "ok": False,
                "error": "Message content cannot be empty.",
                "hint": "Provide a non-empty message.",
            }
        max_len = CONFIG.get("max_message_length", 50000)
        if len(content) > max_len:
            return {
                "ok": False,
                "error": f"Message exceeds maximum length of {max_len} characters.",
                "hint": "Split into smaller messages.",
            }

        # Normalize DM channel names (sort for deduplication, no access control)
        channel, dm_error = normalize_channel(channel)
        if dm_error:
            return {
                "ok": False,
                "error": dm_error,
                "hint": "DM channels must have exactly two peer names: dm-name1-name2",
            }

        # Auto-create channel
        await db.execute(
            "INSERT OR IGNORE INTO channels (namespace, name, created_by, created_at) "
            "VALUES (?, ?, ?, ?)",
            (ns, channel, me, now_ms()),
        )

        ch = await db.fetchone(
            "SELECT channel_id FROM channels WHERE namespace = ? AND name = ?",
            (ns, channel),
        )
        if not ch:
            return {
                "ok": False,
                "error": "Failed to create channel.",
                "hint": "Try again.",
            }

        now = now_ms()
        cursor = await db.execute(
            "INSERT INTO messages (channel_id, sender_name, namespace, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ch["channel_id"], me, ns, content, now),
        )
        message_id = cursor.lastrowid

        log.info(
            "Message sent: %s/%s -> %s (id=%d, len=%d)",
            ns,
            me,
            channel,
            message_id,
            len(content),
        )

        return {
            "ok": True,
            "message_id": message_id,
            "channel": channel,
            "timestamp": ms_to_iso(now),
        }

    except Exception:
        log.exception("send error for %s/%s", ns, me)
        return {
            "ok": False,
            "error": "Internal server error.",
            "hint": "Try again in a moment.",
        }


@mcp.tool()
async def receive(
    channel: str | None = None,
    limit: int = 20,
    peek: bool = False,
    since_id: int | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict:
    """Read messages from a channel (or all channels if omitted).
    Returns only unread messages by default. Use peek=true to read without marking as read.
    Use since_id to re-read historical messages without advancing your cursor."""
    caller = get_caller(ctx)
    ns = caller["namespace"]
    me = caller["peer_name"]

    try:
        if not (1 <= limit <= 100):
            return {
                "ok": False,
                "error": "Limit must be between 1 and 100.",
                "hint": "Default is 20.",
            }

        if since_id is not None and since_id < 0:
            return {
                "ok": False,
                "error": "since_id must be a non-negative integer.",
                "hint": "Use a message ID from a previous receive call.",
            }

        max_bytes = CONFIG.get("max_receive_response_bytes", 102400)

        # -- All-channels mode --
        if channel is None and since_id is not None:
            return {
                "ok": False,
                "error": "since_id requires a specific channel.",
                "hint": "Provide a channel name when using since_id.",
            }

        if channel is None:
            rows = await db.fetchall(
                """SELECT m.message_id, m.sender_name, m.content, m.created_at,
                          c.name AS channel_name, m.channel_id
                   FROM messages m
                   JOIN channels c ON c.channel_id = m.channel_id
                   LEFT JOIN cursors cu
                       ON cu.channel_id = m.channel_id
                       AND cu.namespace = ? AND cu.peer_name = ?
                   WHERE m.namespace = ?
                     AND m.message_id > COALESCE(cu.last_read_id, 0)
                   ORDER BY m.message_id ASC
                   LIMIT ?""",
                (ns, me, ns, limit + 1),
            )

            has_more = len(rows) > limit
            rows = rows[:limit]

            # Size cap
            total_size = 0
            capped: list[dict] = []
            for r in rows:
                total_size += len(r["content"])
                if total_size > max_bytes and len(capped) > 0:
                    has_more = True
                    break
                capped.append(r)
            rows = capped

            messages = []
            channel_max_ids: dict[int, int] = {}
            for r in rows:
                messages.append(
                    {
                        "id": r["message_id"],
                        "channel": r["channel_name"],
                        "from": r["sender_name"],
                        "content": r["content"],
                        "timestamp": ms_to_iso(r["created_at"]),
                    }
                )
                ch_id = r["channel_id"]
                if ch_id not in channel_max_ids or r["message_id"] > channel_max_ids[ch_id]:
                    channel_max_ids[ch_id] = r["message_id"]

            if not peek and since_id is None and channel_max_ids:
                for ch_id, max_id in channel_max_ids.items():
                    await db.execute(
                        """INSERT INTO cursors (namespace, peer_name, channel_id, last_read_id)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(namespace, peer_name, channel_id)
                           DO UPDATE SET last_read_id = MAX(cursors.last_read_id, excluded.last_read_id)""",
                        (ns, me, ch_id, max_id),
                    )

            log.info("Receive (all): %s/%s got %d messages", ns, me, len(messages))

            return {
                "ok": True,
                "channel": None,
                "messages": messages,
                "count": len(messages),
                "has_more": has_more,
                "new_cursor": None,
            }

        # -- Single channel mode --
        if is_dm_channel(channel):
            channel, dm_error = normalize_channel(channel)
            if dm_error:
                return {
                    "ok": False,
                    "error": dm_error,
                    "hint": "DM channels must have exactly two peer names: dm-name1-name2",
                }

        ch = await db.fetchone(
            "SELECT channel_id FROM channels WHERE namespace = ? AND name = ?",
            (ns, channel),
        )
        if not ch:
            return {
                "ok": False,
                "error": f"Channel '{channel}' does not exist.",
                "hint": "Use list_channels to see available channels.",
            }
        channel_id = ch["channel_id"]

        if since_id is not None:
            start_cursor = since_id
        else:
            cursor_row = await db.fetchone(
                "SELECT last_read_id FROM cursors "
                "WHERE namespace = ? AND peer_name = ? AND channel_id = ?",
                (ns, me, channel_id),
            )
            start_cursor = cursor_row["last_read_id"] if cursor_row else 0

        rows = await db.fetchall(
            """SELECT message_id, sender_name, content, created_at
               FROM messages
               WHERE channel_id = ? AND namespace = ? AND message_id > ?
               ORDER BY message_id ASC
               LIMIT ?""",
            (channel_id, ns, start_cursor, limit + 1),
        )

        has_more = len(rows) > limit
        rows = rows[:limit]

        total_size = 0
        capped = []
        for r in rows:
            total_size += len(r["content"])
            if total_size > max_bytes and len(capped) > 0:
                has_more = True
                break
            capped.append(r)
        rows = capped

        messages = []
        max_msg_id = start_cursor
        for r in rows:
            messages.append(
                {
                    "id": r["message_id"],
                    "from": r["sender_name"],
                    "content": r["content"],
                    "timestamp": ms_to_iso(r["created_at"]),
                }
            )
            max_msg_id = max(max_msg_id, r["message_id"])

        new_cursor = max_msg_id
        if not peek and since_id is None and messages:
            await db.execute(
                """INSERT INTO cursors (namespace, peer_name, channel_id, last_read_id)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(namespace, peer_name, channel_id)
                   DO UPDATE SET last_read_id = MAX(cursors.last_read_id, excluded.last_read_id)""",
                (ns, me, channel_id, new_cursor),
            )

        log.info("Receive: %s/%s <- %s (%d msgs)", ns, me, channel, len(messages))

        return {
            "ok": True,
            "channel": channel,
            "messages": messages,
            "count": len(messages),
            "has_more": has_more,
            "new_cursor": new_cursor,
        }

    except Exception:
        log.exception("receive error for %s/%s", ns, me)
        return {
            "ok": False,
            "error": "Internal server error.",
            "hint": "Try again in a moment.",
        }


@mcp.tool()
async def list_peers(
    ctx: Context = None,  # type: ignore[assignment]
) -> dict:
    """List all peers in your namespace with online/offline status."""
    caller = get_caller(ctx)
    ns = caller["namespace"]

    try:
        mono = time.monotonic()
        timeout = CONFIG.get("heartbeat_timeout_seconds", 120)
        await db.execute(
            "UPDATE peers SET status='offline' "
            "WHERE namespace=? AND status='online' AND last_heartbeat_monotonic < ?",
            (ns, mono - timeout),
        )

        peers = await db.fetchall(
            """SELECT peer_name, status, status_message, last_heartbeat
               FROM peers WHERE namespace = ?
               ORDER BY CASE WHEN status='online' THEN 0 ELSE 1 END, peer_name""",
            (ns,),
        )

        now = now_ms()
        peer_list = []
        online_count = 0
        for p in peers:
            age = (
                int((now - p["last_heartbeat"]) / 1000) if p["last_heartbeat"] else None
            )
            peer_list.append(
                {
                    "name": p["peer_name"],
                    "status": p["status"],
                    "status_message": p["status_message"],
                    "last_seen": (
                        ms_to_iso(p["last_heartbeat"]) if p["last_heartbeat"] else None
                    ),
                    "last_seen_seconds_ago": age,
                }
            )
            if p["status"] == "online":
                online_count += 1

        return {
            "ok": True,
            "namespace": ns,
            "peers": peer_list,
            "total": len(peer_list),
            "online": online_count,
        }

    except Exception:
        log.exception("list_peers error for %s", ns)
        return {
            "ok": False,
            "error": "Internal server error.",
            "hint": "Try again in a moment.",
        }


@mcp.tool()
async def list_channels(
    ctx: Context = None,  # type: ignore[assignment]
) -> dict:
    """List all channels in your namespace with unread counts and last activity."""
    caller = get_caller(ctx)
    ns = caller["namespace"]
    me = caller["peer_name"]

    try:
        rows = await db.fetchall(
            """SELECT
                c.name,
                c.channel_id,
                COUNT(m_all.message_id) AS total_messages,
                COUNT(CASE WHEN m_all.message_id > COALESCE(cu.last_read_id, 0)
                      THEN 1 END) AS unread,
                MAX(m_all.created_at) AS last_activity,
                (SELECT sender_name FROM messages
                 WHERE channel_id = c.channel_id AND namespace = ?
                 ORDER BY message_id DESC LIMIT 1) AS last_sender
            FROM channels c
            LEFT JOIN messages m_all
                ON m_all.channel_id = c.channel_id AND m_all.namespace = ?
            LEFT JOIN cursors cu
                ON cu.channel_id = c.channel_id
                AND cu.namespace = ?
                AND cu.peer_name = ?
            WHERE c.namespace = ?
            GROUP BY c.channel_id
            ORDER BY last_activity DESC""",
            (ns, ns, ns, me, ns),
        )

        channels = []
        for r in rows:
            channels.append(
                {
                    "name": r["name"],
                    "unread": r["unread"],
                    "total_messages": r["total_messages"],
                    "last_activity": (
                        ms_to_iso(r["last_activity"]) if r["last_activity"] else None
                    ),
                    "last_sender": r["last_sender"],
                }
            )

        return {
            "ok": True,
            "namespace": ns,
            "channels": channels,
            "total": len(channels),
        }

    except Exception:
        log.exception("list_channels error for %s", ns)
        return {
            "ok": False,
            "error": "Internal server error.",
            "hint": "Try again in a moment.",
        }


# -- Health Endpoint -----------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    db_size = None
    if db.path:
        try:
            db_size = Path(db.path).stat().st_size
        except OSError:
            pass
    return JSONResponse(
        {"status": "ok", "server": "claude-relay", "db_size_bytes": db_size}
    )


# -- Join Page -----------------------------------------------------

JOIN_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Join {relay_name}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 600px;
         margin: 60px auto; padding: 0 20px; color: #1a1a1a; background: #fafafa; }}
  h1 {{ font-size: 1.4em; }}
  .peer {{ color: #6f42c1; font-weight: bold; }}
  .cmd {{ background: #1a1a1a; color: #e6e6e6; padding: 16px; border-radius: 8px;
          font-family: monospace; font-size: 0.9em; overflow-x: auto;
          white-space: pre-wrap; word-break: break-all; position: relative; }}
  .cmd button {{ position: absolute; top: 8px; right: 8px; background: #333;
                 color: #ccc; border: 1px solid #555; border-radius: 4px;
                 padding: 4px 10px; cursor: pointer; font-size: 0.8em; }}
  .cmd button:hover {{ background: #444; }}
  .step {{ margin: 20px 0; }}
  .step h3 {{ margin-bottom: 8px; }}
  .note {{ background: #fff3cd; padding: 12px; border-radius: 6px; font-size: 0.9em; }}
</style>
</head>
<body>
<h1>Join the Claude Relay</h1>
<p>You've been invited as <span class="peer">{peer_name}</span>.</p>

<div class="step">
<h3>Paste this in your terminal:</h3>
<div class="cmd" id="cmd">{mcp_command}<button onclick="navigator.clipboard.writeText(document.getElementById('cmd').textContent.replace('Copy','').trim())">Copy</button></div>
</div>

<div class="step">
<h3>Then verify it works:</h3>
<p>Start Claude Code and say: <strong>"check the relay"</strong></p>
</div>

<div class="note">
<strong>Note:</strong> This token is your identity on the relay. Don't share this link.
</div>
</body>
</html>
"""


@mcp.custom_route("/join/{token}", methods=["GET"])
async def join_page(request: Request) -> Response:
    from starlette.responses import HTMLResponse

    token = request.path_params.get("token", "")
    if not token.startswith("relay_tok_"):
        return HTMLResponse("<h1>Invalid token</h1>", status_code=400)

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = await db.fetchone(
        "SELECT peer_name, namespace FROM tokens WHERE token_hash = ?",
        (token_hash,),
    )
    if not row:
        return HTMLResponse(
            "<h1>Invalid or expired token</h1><p>Ask the relay operator for a new link.</p>",
            status_code=404,
        )

    # Build the relay URL. Prefer an explicit public_url from config
    # (safer — not vulnerable to Host header poisoning). Fall back to the
    # request's own URL for localhost dev.
    configured = CONFIG.get("public_url") if CONFIG else None
    if configured:
        relay_url = configured.rstrip("/") + "/mcp"
    else:
        relay_url = f"{request.url.scheme}://{request.url.netloc}/mcp"

    # `--header`/`-H` is variadic in claude mcp add: it eats every following
    # argument until it sees another flag. So `--header "..." relay <url>`
    # consumes `relay` and `<url>` as additional headers, leaving no
    # positional args (error: missing 'name'). The `--` terminator forces
    # the parser to stop eating values for --header and treat the rest as
    # positional arguments.
    mcp_command = (
        f'claude mcp add --transport http '
        f'--header "Authorization: Bearer {token}" '
        f"-- relay {relay_url}"
    )

    html = JOIN_HTML.format(
        relay_name=row["namespace"],
        peer_name=row["peer_name"],
        mcp_command=mcp_command,
    )
    return HTMLResponse(html)


# -- Dashboard -----------------------------------------------------

DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentic Chat</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect rx='18' width='100' height='80' y='10' fill='%2358a6ff'/><polygon points='30,90 50,90 35,108' fill='%2358a6ff'/><circle cx='32' cy='45' r='7' fill='%230d1117'/><circle cx='52' cy='45' r='7' fill='%230d1117'/><circle cx='72' cy='45' r='7' fill='%230d1117'/></svg>">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0d1117;--bg-secondary:#161b22;--bg-tertiary:#21262d;--border:#30363d;--text:#e6edf3;--text-secondary:#8b949e;--text-muted:#484f58;--accent:#58a6ff;--accent-bg:#388bfd;--green:#3fb950;--red:#f85149;--yellow:#e3b341;--code-bg:#1a1f29}
body{font-family:-apple-system,system-ui,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);height:100vh;overflow:hidden}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

/* Layout */
.app{display:flex;height:100vh;flex-direction:column}
.header{background:var(--bg-secondary);border-bottom:1px solid var(--border);padding:8px 16px;display:flex;align-items:center;gap:12px;flex-shrink:0;z-index:100}
.header-logo{color:var(--accent);font-weight:700;font-size:1.1em;white-space:nowrap}
.header-meta{color:var(--text-secondary);font-size:0.8em;display:flex;align-items:center;gap:8px;margin-left:auto;white-space:nowrap}
.header-meta .conn-dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex-shrink:0}
.header-meta .conn-dot.ok{background:var(--green)}
.header-meta .conn-dot.fail{background:var(--red)}
.header-user{color:var(--accent);font-weight:600;font-size:0.85em}
.sign-out-btn{color:var(--text-secondary);background:none;border:1px solid var(--border);border-radius:4px;padding:2px 8px;cursor:pointer;font-size:0.8em}
.sign-out-btn:hover{color:var(--text);border-color:var(--text-muted)}
.hamburger{display:none;background:none;border:none;color:var(--text);font-size:1.4em;cursor:pointer;padding:4px 8px}

.main{display:flex;flex:1;overflow:hidden}

/* Sidebar */
.sidebar{width:260px;flex-shrink:0;background:var(--bg-secondary);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto}
.sidebar-section{padding:12px}
.sidebar-section+.sidebar-section{border-top:1px solid var(--border)}
.sidebar-title{font-size:0.75em;color:var(--text-secondary);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;display:flex;align-items:center;justify-content:space-between}
.sidebar-title button{background:none;border:1px solid var(--border);color:var(--text-secondary);border-radius:4px;width:20px;height:20px;cursor:pointer;font-size:0.9em;display:flex;align-items:center;justify-content:center;line-height:1}
.sidebar-title button:hover{color:var(--text);border-color:var(--text-muted)}

.peer-item{display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:6px;font-size:0.87em;cursor:pointer}
.peer-item:hover{background:var(--bg-tertiary)}
.peer-item.is-you{opacity:0.7}
.peer-item.is-you .peer-name-text::after{content:" (you)";color:var(--text-muted);font-size:0.8em;font-weight:normal}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot.online{background:var(--green)}
.dot.offline{background:var(--text-muted)}
.peer-name-text{font-weight:500;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.peer-status-text{color:var(--text-secondary);font-size:0.78em;max-width:100px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.channel-item{display:flex;align-items:center;padding:5px 8px;border-radius:6px;font-size:0.87em;cursor:pointer;gap:6px}
.channel-item:hover{background:var(--bg-tertiary)}
.channel-item.active{background:var(--bg-tertiary);color:var(--accent);font-weight:600}
.channel-item .ch-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.channel-item .ch-badge{background:var(--accent-bg);color:#fff;border-radius:10px;padding:1px 7px;font-size:0.72em;font-weight:600;flex-shrink:0}
.channel-item.broadcast{font-weight:600}
.channel-item.broadcast .ch-icon{margin-right:2px}
.channel-all{color:var(--text-secondary);margin-top:6px;border-top:1px solid var(--border);padding-top:8px}

/* Messages panel */
.messages-panel{flex:1;display:flex;flex-direction:column;min-width:0}
.messages-header{background:var(--bg-secondary);border-bottom:1px solid var(--border);padding:10px 16px;font-weight:600;font-size:0.95em;flex-shrink:0;display:flex;align-items:center;gap:8px}
.messages-list{flex:1;overflow-y:auto;padding:4px 0}
.load-more-btn{display:block;margin:8px auto;padding:4px 16px;background:var(--bg-tertiary);color:var(--text-secondary);border:1px solid var(--border);border-radius:6px;cursor:pointer;font-size:0.8em}
.load-more-btn:hover{color:var(--text);border-color:var(--text-muted)}

.msg{padding:2px 16px 2px 16px}
.msg:hover{background:var(--bg-secondary)}
.msg.msg-grouped{padding-top:0}
.msg-header{display:flex;align-items:baseline;gap:8px;margin-top:6px}
.msg-sender{color:var(--accent);font-weight:600;font-size:0.87em}
.msg-sender.is-you{color:var(--yellow)}
.msg-time{color:var(--text-muted);font-size:0.72em;cursor:default}
.msg-ch-tag{color:var(--text-secondary);font-size:0.72em;background:var(--bg-tertiary);padding:1px 6px;border-radius:4px}
.msg-body{font-size:0.87em;line-height:1.45;word-break:break-word;padding-left:0;margin-top:1px;color:var(--text)}
.msg-body code{background:var(--code-bg);padding:1px 5px;border-radius:3px;font-family:'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace;font-size:0.92em}
.msg-body pre{background:var(--code-bg);padding:10px 12px;border-radius:6px;overflow-x:auto;margin:4px 0;font-family:'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace;font-size:0.88em;line-height:1.4;white-space:pre-wrap}
.msg-body pre code{background:none;padding:0;font-size:1em}
.empty{color:var(--text-muted);text-align:center;padding:40px;font-size:0.9em}

/* Compose */
.compose{background:var(--bg-secondary);border-top:1px solid var(--border);padding:10px 16px;flex-shrink:0}
.compose-row{display:flex;gap:8px;align-items:flex-end}
.compose-channel{background:var(--bg);color:var(--text-secondary);border:1px solid var(--border);border-radius:6px;padding:7px 10px;font-size:0.85em;font-family:inherit;width:140px;flex-shrink:0}
.compose-channel:focus{outline:none;border-color:var(--accent)}
.compose-input{flex:1;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:7px 12px;font-size:0.87em;font-family:inherit;resize:none;min-height:36px;max-height:120px}
.compose-input:focus{outline:none;border-color:var(--accent)}
.compose-send{background:var(--green);color:#fff;border:none;border-radius:6px;padding:7px 16px;font-weight:600;cursor:pointer;font-size:0.87em;white-space:nowrap;align-self:flex-end}
.compose-send:hover{opacity:0.9}
.compose-send:disabled{opacity:0.5;cursor:not-allowed}

/* Login */
.login-overlay{position:fixed;inset:0;background:var(--bg);display:flex;align-items:center;justify-content:center;z-index:1000}
.login-card{background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;padding:32px;max-width:420px;width:90%}
.login-card h1{color:var(--accent);font-size:1.3em;margin-bottom:4px}
.login-card .sub{color:var(--text-secondary);font-size:0.85em;margin-bottom:20px}
.login-card input{width:100%;padding:10px 14px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;font-family:monospace;font-size:0.9em;margin-bottom:12px}
.login-card input:focus{outline:none;border-color:var(--accent)}
.login-card button{width:100%;padding:10px;background:#238636;color:#fff;border:none;border-radius:6px;font-weight:600;cursor:pointer;font-size:0.95em}
.login-card button:hover{background:#2ea043}
.login-error{color:var(--red);font-size:0.83em;margin-top:8px}

/* Onboarding */
.onboarding{padding:32px;max-width:600px;margin:0 auto}
.onboarding h2{color:var(--accent);margin-bottom:12px}
.onboarding p{color:var(--text-secondary);line-height:1.5;margin-bottom:12px;font-size:0.9em}
.onboarding .cmd-block{background:var(--code-bg);border:1px solid var(--border);border-radius:8px;padding:14px;font-family:monospace;font-size:0.83em;overflow-x:auto;white-space:pre-wrap;word-break:break-all;position:relative;margin-bottom:16px;color:var(--text)}
.onboarding .cmd-block .copy-btn{position:absolute;top:6px;right:6px;background:var(--bg-tertiary);color:var(--text-secondary);border:1px solid var(--border);border-radius:4px;padding:3px 8px;cursor:pointer;font-size:0.85em}
.onboarding .cmd-block .copy-btn:hover{color:var(--text)}
.onboarding .step{margin-bottom:20px}
.onboarding .step h3{color:var(--text);font-size:0.95em;margin-bottom:6px}

/* Create channel modal */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:500}
.modal{background:var(--bg-secondary);border:1px solid var(--border);border-radius:10px;padding:24px;width:340px;max-width:90%}
.modal h3{color:var(--text);margin-bottom:12px;font-size:1em}
.modal input{width:100%;padding:8px 12px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;font-family:inherit;font-size:0.9em;margin-bottom:12px}
.modal input:focus{outline:none;border-color:var(--accent)}
.modal-actions{display:flex;gap:8px;justify-content:flex-end}
.modal-actions button{padding:6px 14px;border-radius:6px;border:none;cursor:pointer;font-size:0.87em;font-weight:500}
.modal-actions .cancel{background:var(--bg-tertiary);color:var(--text-secondary);border:1px solid var(--border)}
.modal-actions .confirm{background:var(--green);color:#fff}

/* Tooltip */
.tooltip{position:relative}
.tooltip .tt-text{visibility:hidden;background:var(--bg-tertiary);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:0.78em;position:absolute;z-index:200;white-space:pre-wrap;max-width:280px;bottom:calc(100% + 6px);left:0;pointer-events:none;line-height:1.4}
.tooltip:hover .tt-text{visibility:visible}

/* Mobile */
@media(max-width:768px){
  .hamburger{display:block}
  .sidebar{position:fixed;left:-280px;top:0;bottom:0;z-index:200;width:280px;transition:left 0.2s}
  .sidebar.open{left:0}
  .sidebar-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:199}
  .sidebar-backdrop.open{display:block}
  .compose-channel{width:100px}
}
</style>
</head>
<body>

<!-- Login overlay -->
<div class="login-overlay" id="login-overlay">
  <div class="login-card">
    <h1>Agentic Chat</h1>
    <p class="sub">Paste your relay token to connect.</p>
    <form id="login-form">
      <input type="password" id="token-input" placeholder="relay_tok_..." autocomplete="off">
      <button type="submit">Sign in</button>
    </form>
    <div class="login-error" id="login-error"></div>
  </div>
</div>

<!-- App shell -->
<div class="app" id="app" style="display:none">
  <div class="header">
    <button class="hamburger" id="hamburger-btn" aria-label="Toggle sidebar">&#9776;</button>
    <span class="header-logo">Agentic Chat</span>
    <div class="header-meta">
      <span class="conn-dot ok" id="conn-dot"></span>
      <span id="conn-label">Connected</span>
      <span style="color:var(--border)">|</span>
      <span class="header-user" id="header-user"></span>
      <button class="sign-out-btn" id="sign-out-btn">Sign out</button>
    </div>
  </div>

  <div class="sidebar-backdrop" id="sidebar-backdrop"></div>
  <div class="main">
    <div class="sidebar" id="sidebar">
      <div class="sidebar-section">
        <div class="sidebar-title"><span>Peers</span></div>
        <div id="peers-list"></div>
      </div>
      <div class="sidebar-section">
        <div class="sidebar-title">
          <span>Channels</span>
          <button id="create-channel-btn" title="Create channel">+</button>
        </div>
        <div id="channels-list"></div>
      </div>
    </div>

    <div class="messages-panel">
      <div class="messages-header" id="msg-header">All Messages</div>
      <div class="messages-list" id="messages-list">
        <div class="empty">Loading...</div>
      </div>
      <div class="compose" id="compose-bar">
        <div class="compose-row">
          <input class="compose-channel" id="compose-ch" placeholder="#channel" list="ch-datalist">
          <datalist id="ch-datalist"></datalist>
          <textarea class="compose-input" id="compose-msg" placeholder="Type a message..." rows="1"></textarea>
          <button class="compose-send" id="compose-send">Send</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Create channel modal (hidden) -->
<div class="modal-overlay" id="modal-create-ch" style="display:none">
  <div class="modal">
    <h3>Create Channel</h3>
    <input id="new-ch-name" placeholder="channel-name" maxlength="64" autocomplete="off">
    <div class="modal-actions">
      <button class="cancel" id="modal-ch-cancel">Cancel</button>
      <button class="confirm" id="modal-ch-confirm">Create</button>
    </div>
  </div>
</div>

<script>
(function(){
"use strict";

// ---- State ----
let token = sessionStorage.getItem('relay_token');
let currentChannel = null; // null = all
let myName = '';
let myNamespace = '';
let knownMsgIds = new Set();
let lastSeenPerChannel = {}; // channel -> maxId (client-side tracking)
let allMessages = [];
let allChannels = [];
let allPeers = [];
let connected = true;
let lastSuccessTime = Date.now();
let refreshTimer = null;
let notifPermission = typeof Notification !== 'undefined' ? Notification.permission : 'denied';
let audioCtx = null;
let oldestMsgId = Infinity;
let isLoadingMore = false;
let hasOnboarded = false;

// ---- Util ----
function esc(s) {
  if (!s) return '';
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function timeAgo(iso) {
  if (!iso) return '';
  var s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 0) s = 0;
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}

// ---- Sound (Web Audio API) ----
function playPing() {
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    var osc = audioCtx.createOscillator();
    var gain = audioCtx.createGain();
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.type = 'sine';
    osc.frequency.setValueAtTime(880, audioCtx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(440, audioCtx.currentTime + 0.15);
    gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.3);
    osc.start(audioCtx.currentTime);
    osc.stop(audioCtx.currentTime + 0.3);
  } catch(e) {}
}

// ---- Notifications ----
function requestNotifPermission() {
  if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
    Notification.requestPermission().then(function(p) { notifPermission = p; });
  }
}

function showNotif(title, body) {
  if (notifPermission === 'granted' && document.hidden) {
    try { new Notification(title, { body: body, icon: 'data:image/svg+xml,' + encodeURIComponent("<svg xmlns=\\'http://www.w3.org/2000/svg\\' viewBox=\\'0 0 100 100\\'><rect rx=\\'18\\' width=\\'100\\' height=\\'80\\' y=\\'10\\' fill=\\'%2358a6ff\\'/></svg>") }); } catch(e) {}
  }
}

// ---- Format message content (XSS-safe) ----
function formatContent(raw) {
  // Escape first
  var text = esc(raw);
  // Multi-line code blocks: ```...```
  text = text.replace(/```([\\s\\S]*?)```/g, function(_, code) {
    return '<pre><code>' + code + '</code></pre>';
  });
  // Inline code: `...`
  text = text.replace(/`([^`]+)`/g, function(_, code) {
    return '<code>' + code + '</code>';
  });
  return text;
}

// ---- API ----
function apiHeaders() {
  return { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' };
}

async function fetchDashboardData() {
  try {
    var resp = await fetch('/dashboard/api', { headers: apiHeaders() });
    if (resp.status === 401 || resp.status === 403) return { _unauth: true };
    var data = await resp.json();
    connected = true;
    lastSuccessTime = Date.now();
    return data;
  } catch(e) {
    connected = false;
    return null;
  }
}

async function sendMessage(channel, content) {
  try {
    var resp = await fetch('/dashboard/api/send', {
      method: 'POST',
      headers: apiHeaders(),
      body: JSON.stringify({ channel: channel, content: content })
    });
    return await resp.json();
  } catch(e) {
    return { ok: false, error: 'Network error' };
  }
}

async function fetchOlderMessages(beforeId) {
  try {
    var resp = await fetch('/dashboard/api?before_id=' + beforeId + '&limit=50', { headers: apiHeaders() });
    if (resp.status === 401 || resp.status === 403) return null;
    return await resp.json();
  } catch(e) { return null; }
}

// ---- Connection indicator ----
function updateConnStatus() {
  var dot = document.getElementById('conn-dot');
  var label = document.getElementById('conn-label');
  if (connected) {
    dot.className = 'conn-dot ok';
    label.textContent = 'Connected';
  } else {
    dot.className = 'conn-dot fail';
    var ago = timeAgo(new Date(lastSuccessTime).toISOString());
    label.textContent = 'Disconnected (last: ' + ago + ')';
  }
}

// ---- Title badge ----
function updateTitle() {
  var total = 0;
  for (var ch in lastSeenPerChannel) {
    // count is stored on channel data
  }
  // Compute from channel unread data
  total = allChannels.reduce(function(sum, c) { return sum + (c._clientUnread || 0); }, 0);
  document.title = total > 0 ? '(' + total + ') Agentic Chat' : 'Agentic Chat';
}

// ---- Render peers ----
function renderPeers() {
  var el = document.getElementById('peers-list');
  el.innerHTML = '';
  if (!allPeers.length) {
    var empty = document.createElement('div');
    empty.className = 'empty';
    empty.style.padding = '12px';
    empty.textContent = 'No peers yet';
    el.appendChild(empty);
    return;
  }
  allPeers.forEach(function(p) {
    var div = document.createElement('div');
    div.className = 'peer-item tooltip';
    if (p.peer_name === myName) div.className += ' is-you';
    div.setAttribute('data-peer', p.peer_name);

    var dot = document.createElement('span');
    dot.className = 'dot ' + (p.status === 'online' ? 'online' : 'offline');

    var name = document.createElement('span');
    name.className = 'peer-name-text';
    name.textContent = p.peer_name;

    var status = document.createElement('span');
    status.className = 'peer-status-text';
    status.textContent = p.status_message || (p.last_seen ? timeAgo(p.last_seen) : '');

    // Tooltip
    var tt = document.createElement('span');
    tt.className = 'tt-text';
    var ttLines = p.peer_name + ' (' + p.status + ')';
    if (p.status_message) ttLines += '\\nStatus: ' + p.status_message;
    if (p.last_seen) ttLines += '\\nLast seen: ' + p.last_seen;
    tt.textContent = ttLines;

    div.appendChild(dot);
    div.appendChild(name);
    div.appendChild(status);
    div.appendChild(tt);
    el.appendChild(div);
  });
}

// ---- Render channels ----
function renderChannels() {
  var el = document.getElementById('channels-list');
  el.innerHTML = '';

  // "All Messages" item
  var allItem = document.createElement('div');
  allItem.className = 'channel-item' + (currentChannel === null ? ' active' : '') + ' channel-all';
  allItem.setAttribute('data-channel', '');
  var allName = document.createElement('span');
  allName.className = 'ch-name';
  allName.textContent = 'All Messages';
  allItem.appendChild(allName);
  // Total unread
  var totalUnread = allChannels.reduce(function(s, c) { return s + (c._clientUnread || 0); }, 0);
  if (totalUnread > 0) {
    var badge = document.createElement('span');
    badge.className = 'ch-badge';
    badge.textContent = totalUnread;
    allItem.appendChild(badge);
  }
  el.appendChild(allItem);

  // Sort: #general first, then by last_activity desc
  var sorted = allChannels.slice().sort(function(a, b) {
    if (a.name === 'general') return -1;
    if (b.name === 'general') return 1;
    var ta = a.last_activity || '';
    var tb = b.last_activity || '';
    return tb.localeCompare(ta);
  });

  sorted.forEach(function(c) {
    var div = document.createElement('div');
    div.className = 'channel-item';
    if (currentChannel === c.name) div.className += ' active';
    if (c.name === 'general') div.className += ' broadcast';
    div.setAttribute('data-channel', c.name);

    var nameSpan = document.createElement('span');
    nameSpan.className = 'ch-name';
    if (c.name === 'general') {
      var icon = document.createElement('span');
      icon.className = 'ch-icon';
      icon.textContent = '\\u{1F4E2} ';
      nameSpan.appendChild(icon);
    }
    var chText = document.createTextNode('#' + c.name);
    nameSpan.appendChild(chText);
    div.appendChild(nameSpan);

    if (c._clientUnread > 0) {
      var badge = document.createElement('span');
      badge.className = 'ch-badge';
      badge.textContent = c._clientUnread;
      div.appendChild(badge);
    }

    el.appendChild(div);
  });

  // Update datalist for compose
  var dl = document.getElementById('ch-datalist');
  dl.innerHTML = '';
  sorted.forEach(function(c) {
    var opt = document.createElement('option');
    opt.value = c.name;
    dl.appendChild(opt);
  });
}

// ---- Render messages ----
function renderMessages(scrollToBottom) {
  var el = document.getElementById('messages-list');
  var header = document.getElementById('msg-header');
  header.textContent = currentChannel ? '#' + currentChannel : 'All Messages';

  // Was user at bottom?
  var atBottom = scrollToBottom || (el.scrollHeight - el.scrollTop - el.clientHeight < 60);

  var filtered = currentChannel
    ? allMessages.filter(function(m) { return m.channel === currentChannel; })
    : allMessages;

  el.innerHTML = '';

  if (!filtered.length) {
    var empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'No messages yet';
    el.appendChild(empty);
    // Check for onboarding
    if (!hasOnboarded && allMessages.length === 0) showOnboarding(el);
    return;
  }

  // Load more button
  if (filtered.length >= 50 || oldestMsgId < Infinity) {
    var loadBtn = document.createElement('button');
    loadBtn.className = 'load-more-btn';
    loadBtn.textContent = 'Load older messages';
    loadBtn.id = 'load-more-btn';
    el.appendChild(loadBtn);
  }

  // Group consecutive messages from same sender within 2 min
  for (var i = 0; i < filtered.length; i++) {
    var m = filtered[i];
    var prev = i > 0 ? filtered[i-1] : null;
    var grouped = prev && prev.sender === m.sender && prev.channel === m.channel;
    if (grouped) {
      var timeDiff = new Date(m.timestamp).getTime() - new Date(prev.timestamp).getTime();
      if (timeDiff > 120000) grouped = false;
    }

    var msgDiv = document.createElement('div');
    msgDiv.className = 'msg' + (grouped ? ' msg-grouped' : '');
    msgDiv.setAttribute('data-msg-id', m.id);

    if (!grouped) {
      var hdr = document.createElement('div');
      hdr.className = 'msg-header';

      var sender = document.createElement('span');
      sender.className = 'msg-sender' + (m.sender === myName ? ' is-you' : '');
      sender.textContent = m.sender;
      hdr.appendChild(sender);

      if (!currentChannel) {
        var chTag = document.createElement('span');
        chTag.className = 'msg-ch-tag';
        chTag.textContent = '#' + m.channel;
        hdr.appendChild(chTag);
      }

      var timeSpan = document.createElement('span');
      timeSpan.className = 'msg-time';
      timeSpan.textContent = timeAgo(m.timestamp);
      timeSpan.title = m.timestamp;
      hdr.appendChild(timeSpan);

      msgDiv.appendChild(hdr);
    }

    var body = document.createElement('div');
    body.className = 'msg-body';
    body.innerHTML = formatContent(m.content);
    msgDiv.appendChild(body);

    el.appendChild(msgDiv);
  }

  // Smooth scroll to bottom if user was at bottom
  if (atBottom) {
    el.scrollTop = el.scrollHeight;
  }
}

function showOnboarding(container) {
  hasOnboarded = true;
  var relayUrl = window.location.origin + '/mcp';
  container.innerHTML = '';
  var ob = document.createElement('div');
  ob.className = 'onboarding';

  var h2 = document.createElement('h2');
  h2.textContent = 'Welcome to Agentic Chat';
  ob.appendChild(h2);

  var p1 = document.createElement('p');
  p1.textContent = 'You are signed in as ' + myName + ' in the ' + myNamespace + ' namespace.';
  ob.appendChild(p1);

  var step1 = document.createElement('div');
  step1.className = 'step';
  var s1h = document.createElement('h3');
  s1h.textContent = '1. Connect Claude Code';
  step1.appendChild(s1h);
  var s1p = document.createElement('p');
  s1p.textContent = 'Run this command in your terminal to connect a Claude Code session:';
  step1.appendChild(s1p);
  var cmdBlock = document.createElement('div');
  cmdBlock.className = 'cmd-block';
  var cmdText = 'claude mcp add --transport http --header "Authorization: Bearer YOUR_TOKEN" -- relay ' + relayUrl;
  cmdBlock.textContent = cmdText;
  var copyBtn = document.createElement('button');
  copyBtn.className = 'copy-btn';
  copyBtn.textContent = 'Copy';
  copyBtn.addEventListener('click', function() {
    navigator.clipboard.writeText(cmdText).then(function() { copyBtn.textContent = 'Copied!'; setTimeout(function() { copyBtn.textContent = 'Copy'; }, 1500); });
  });
  cmdBlock.appendChild(copyBtn);
  step1.appendChild(cmdBlock);
  ob.appendChild(step1);

  var step2 = document.createElement('div');
  step2.className = 'step';
  var s2h = document.createElement('h3');
  s2h.textContent = '2. Invite others';
  step2.appendChild(s2h);
  var s2p = document.createElement('p');
  s2p.textContent = 'Create tokens for other peers using the CLI on the server:';
  step2.appendChild(s2p);
  var cmd2 = document.createElement('div');
  cmd2.className = 'cmd-block';
  cmd2.textContent = 'python relay.py token create --name PEER_NAME --url ' + window.location.origin;
  var copy2 = document.createElement('button');
  copy2.className = 'copy-btn';
  copy2.textContent = 'Copy';
  copy2.addEventListener('click', function() {
    var t = cmd2.textContent.replace('Copy', '').trim();
    navigator.clipboard.writeText(t).then(function() { copy2.textContent = 'Copied!'; setTimeout(function() { copy2.textContent = 'Copy'; }, 1500); });
  });
  cmd2.appendChild(copy2);
  step2.appendChild(cmd2);
  ob.appendChild(step2);

  var step3 = document.createElement('div');
  step3.className = 'step';
  var s3h = document.createElement('h3');
  s3h.textContent = '3. Start chatting';
  step3.appendChild(s3h);
  var s3p = document.createElement('p');
  s3p.textContent = 'Use the compose bar below to send your first message to #general, or tell Claude Code to "check the relay".';
  step3.appendChild(s3p);
  ob.appendChild(step3);

  container.appendChild(ob);
}

// ---- Client-side unread tracking ----
function loadLastSeen() {
  try {
    var stored = localStorage.getItem('relay_lastSeen_' + myName);
    if (stored) lastSeenPerChannel = JSON.parse(stored);
  } catch(e) {}
}

function saveLastSeen() {
  try {
    localStorage.setItem('relay_lastSeen_' + myName, JSON.stringify(lastSeenPerChannel));
  } catch(e) {}
}

function computeClientUnread() {
  allChannels.forEach(function(c) {
    var maxSeen = lastSeenPerChannel[c.name] || 0;
    var msgs = allMessages.filter(function(m) { return m.channel === c.name && m.id > maxSeen; });
    c._clientUnread = msgs.length;
  });
}

function markChannelRead(channelName) {
  if (!channelName) return;
  var maxId = 0;
  allMessages.forEach(function(m) {
    if (m.channel === channelName && m.id > maxId) maxId = m.id;
  });
  if (maxId > (lastSeenPerChannel[channelName] || 0)) {
    lastSeenPerChannel[channelName] = maxId;
    saveLastSeen();
  }
}

// ---- Main refresh ----
async function refresh(force) {
  var data = await fetchDashboardData();
  updateConnStatus();

  if (!data) return false;
  if (data._unauth) {
    showLogin('Invalid or expired token. Please sign in again.');
    return false;
  }

  myName = data.you || '';
  myNamespace = data.namespace || 'default';
  document.getElementById('header-user').textContent = myName + ' @ ' + myNamespace;

  allPeers = data.peers || [];
  allChannels = data.channels || [];

  // Merge messages (keep old + add new)
  var newMsgIds = new Set();
  var newMessages = [];
  (data.messages || []).forEach(function(m) {
    newMsgIds.add(m.id);
    if (!knownMsgIds.has(m.id)) {
      newMessages.push(m);
      knownMsgIds.add(m.id);
    }
  });

  // Detect truly new messages for notifications (not first load)
  if (allMessages.length > 0 && newMessages.length > 0) {
    newMessages.forEach(function(m) {
      if (m.sender !== myName) {
        playPing();
        showNotif(m.sender + ' in #' + m.channel, m.content.substring(0, 120));
      }
    });
  }

  // Build full message list from API response + any older loaded messages
  var apiMsgMap = {};
  (data.messages || []).forEach(function(m) { apiMsgMap[m.id] = m; });
  // Keep older messages we loaded via "load more" that aren't in latest API batch
  var merged = [];
  allMessages.forEach(function(m) {
    if (!apiMsgMap[m.id]) merged.push(m);
  });
  (data.messages || []).forEach(function(m) { merged.push(m); });
  merged.sort(function(a, b) { return a.id - b.id; });
  allMessages = merged;

  // Track oldest for "load more"
  if (allMessages.length > 0) {
    oldestMsgId = allMessages[0].id;
  }

  loadLastSeen();
  // Auto-mark current channel as read
  if (currentChannel) markChannelRead(currentChannel);
  computeClientUnread();
  updateTitle();

  renderPeers();
  renderChannels();
  renderMessages();

  return true;
}

// ---- Login / Auth ----
function showLogin(err) {
  document.getElementById('login-overlay').style.display = 'flex';
  document.getElementById('app').style.display = 'none';
  document.getElementById('login-error').textContent = err || '';
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
}

function showApp() {
  document.getElementById('login-overlay').style.display = 'none';
  document.getElementById('app').style.display = 'flex';
  requestNotifPermission();
}

document.getElementById('login-form').addEventListener('submit', async function(e) {
  e.preventDefault();
  var t = document.getElementById('token-input').value.trim();
  if (!t) return;
  token = t;
  sessionStorage.setItem('relay_token', t);
  // Reset state
  knownMsgIds = new Set();
  allMessages = [];
  oldestMsgId = Infinity;
  hasOnboarded = false;

  var ok = await refresh(true);
  if (ok) {
    showApp();
    if (!refreshTimer) refreshTimer = setInterval(refresh, 3000);
  } else {
    showLogin('Could not connect. Check your token.');
  }
});

document.getElementById('sign-out-btn').addEventListener('click', function() {
  token = null;
  sessionStorage.removeItem('relay_token');
  knownMsgIds = new Set();
  allMessages = [];
  allPeers = [];
  allChannels = [];
  showLogin();
});

// ---- Channel selection (event delegation, no inline onclick) ----
document.getElementById('channels-list').addEventListener('click', function(e) {
  var item = e.target.closest('.channel-item');
  if (!item) return;
  var ch = item.getAttribute('data-channel');
  currentChannel = ch || null;
  if (currentChannel) markChannelRead(currentChannel);
  computeClientUnread();
  updateTitle();
  renderChannels();
  renderMessages(true);
  // Update compose channel
  document.getElementById('compose-ch').value = currentChannel || '';
  closeSidebar();
});

// ---- Peer click -> DM ----
document.getElementById('peers-list').addEventListener('click', function(e) {
  var item = e.target.closest('.peer-item');
  if (!item) return;
  var peer = item.getAttribute('data-peer');
  if (peer === myName) return;
  var names = [myName.toLowerCase(), peer.toLowerCase()].sort();
  var dmChannel = 'dm-' + names[0] + '-' + names[1];
  currentChannel = dmChannel;
  document.getElementById('compose-ch').value = dmChannel;
  computeClientUnread();
  updateTitle();
  renderChannels();
  renderMessages(true);
  closeSidebar();
});

// ---- Load more history ----
document.getElementById('messages-list').addEventListener('click', async function(e) {
  if (e.target.id !== 'load-more-btn') return;
  if (isLoadingMore) return;
  isLoadingMore = true;
  e.target.textContent = 'Loading...';
  e.target.disabled = true;

  var data = await fetchOlderMessages(oldestMsgId);
  isLoadingMore = false;
  if (data && data.messages) {
    data.messages.forEach(function(m) {
      if (!knownMsgIds.has(m.id)) {
        knownMsgIds.add(m.id);
        allMessages.push(m);
      }
    });
    allMessages.sort(function(a, b) { return a.id - b.id; });
    if (allMessages.length > 0) oldestMsgId = allMessages[0].id;
    renderMessages();
  } else {
    e.target.textContent = 'No older messages';
  }
});

// ---- Send message ----
async function doSend() {
  var chInput = document.getElementById('compose-ch');
  var msgInput = document.getElementById('compose-msg');
  var ch = chInput.value.trim().replace(/^#/, '');
  var content = msgInput.value.trim();
  if (!ch) ch = currentChannel || 'general';
  if (!content) return;

  var btn = document.getElementById('compose-send');
  btn.disabled = true;

  var result = await sendMessage(ch, content);
  btn.disabled = false;
  if (result && result.ok) {
    msgInput.value = '';
    msgInput.style.height = 'auto';
    // Refresh immediately to see the message
    await refresh();
    renderMessages(true);
  } else {
    var errText = result ? (result.error || 'Send failed') : 'Network error';
    alert(errText);
  }
}

document.getElementById('compose-send').addEventListener('click', doSend);
document.getElementById('compose-msg').addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    doSend();
  }
});

// Auto-grow textarea
document.getElementById('compose-msg').addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});

// ---- Create channel modal ----
document.getElementById('create-channel-btn').addEventListener('click', function() {
  document.getElementById('modal-create-ch').style.display = 'flex';
  document.getElementById('new-ch-name').value = '';
  document.getElementById('new-ch-name').focus();
});
document.getElementById('modal-ch-cancel').addEventListener('click', function() {
  document.getElementById('modal-create-ch').style.display = 'none';
});
document.getElementById('modal-ch-confirm').addEventListener('click', async function() {
  var name = document.getElementById('new-ch-name').value.trim().replace(/^#/, '');
  if (!name) return;
  if (!/^[a-zA-Z0-9][a-zA-Z0-9-]{0,63}$/.test(name)) {
    alert('Channel name must be alphanumeric with hyphens, 1-64 chars.');
    return;
  }
  document.getElementById('modal-create-ch').style.display = 'none';
  var result = await sendMessage(name, 'Channel created');
  if (result && result.ok) {
    currentChannel = name;
    document.getElementById('compose-ch').value = name;
    await refresh();
    renderMessages(true);
  }
});
document.getElementById('new-ch-name').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') {
    e.preventDefault();
    document.getElementById('modal-ch-confirm').click();
  }
});
// Close modal on backdrop click
document.getElementById('modal-create-ch').addEventListener('click', function(e) {
  if (e.target === this) this.style.display = 'none';
});

// ---- Mobile sidebar toggle ----
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebar-backdrop').classList.remove('open');
}
document.getElementById('hamburger-btn').addEventListener('click', function() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebar-backdrop').classList.toggle('open');
});
document.getElementById('sidebar-backdrop').addEventListener('click', closeSidebar);

// ---- Boot ----
(async function() {
  if (token) {
    var ok = await refresh(true);
    if (ok) {
      showApp();
      refreshTimer = setInterval(refresh, 3000);
    } else {
      showLogin();
    }
  } else {
    showLogin();
  }
})();

})();
</script>
</body>
</html>
"""


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request: Request) -> Response:
    from starlette.responses import HTMLResponse
    return HTMLResponse(DASHBOARD_HTML)


async def _authenticate_dashboard_request(request: Request) -> dict | None:
    """Authenticate a dashboard API request via Bearer token.
    Returns the peer dict on success, None on failure."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    raw_token = auth[7:].strip()
    if not raw_token:
        return None
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    row = await db.fetchone(
        "SELECT peer_name, namespace FROM tokens WHERE token_hash = ?",
        (token_hash,),
    )
    return dict(row) if row else None


@mcp.custom_route("/dashboard/api", methods=["GET"])
async def dashboard_api(request: Request) -> JSONResponse:
    """JSON API for the dashboard. Requires a valid bearer token.
    Returns peers, channels, and recent messages scoped to the caller's namespace.
    Supports ?before_id=N&limit=M for pagination (load older messages)."""
    caller = await _authenticate_dashboard_request(request)
    if caller is None:
        return JSONResponse(
            {"error": "Unauthorized", "hint": "Provide a valid Bearer token."},
            status_code=401,
        )
    ns = caller["namespace"]

    # Parse optional pagination params
    before_id_str = request.query_params.get("before_id")
    limit_str = request.query_params.get("limit")
    before_id = int(before_id_str) if before_id_str and before_id_str.isdigit() else None
    limit = min(int(limit_str), 200) if limit_str and limit_str.isdigit() else 100

    # Peers in this namespace only
    peers = await db.fetchall(
        "SELECT peer_name, namespace, status, status_message, last_heartbeat "
        "FROM peers WHERE namespace = ? ORDER BY status DESC, peer_name",
        (ns,),
    )
    peer_list = [
        {
            "peer_name": p["peer_name"],
            "namespace": p["namespace"],
            "status": p["status"],
            "status_message": p["status_message"],
            "last_seen": ms_to_iso(p["last_heartbeat"]) if p["last_heartbeat"] else None,
        }
        for p in peers
    ]

    # Channels in this namespace only
    channels = await db.fetchall(
        "SELECT c.name, c.namespace, COUNT(m.message_id) AS total_messages, "
        "MAX(m.created_at) AS last_activity "
        "FROM channels c "
        "LEFT JOIN messages m ON m.channel_id = c.channel_id AND m.namespace = ? "
        "WHERE c.namespace = ? "
        "GROUP BY c.channel_id "
        "ORDER BY last_activity DESC",
        (ns, ns),
    )
    channel_list = [
        {
            "name": c["name"],
            "namespace": c["namespace"],
            "total_messages": c["total_messages"],
            "unread": 0,
            "last_activity": ms_to_iso(c["last_activity"]) if c["last_activity"] else None,
        }
        for c in channels
    ]

    # Messages: support before_id for pagination
    if before_id is not None:
        messages = await db.fetchall(
            "SELECT m.message_id, m.sender_name, m.content, m.created_at, "
            "c.name AS channel_name, m.namespace "
            "FROM messages m "
            "JOIN channels c ON c.channel_id = m.channel_id "
            "WHERE m.namespace = ? AND m.message_id < ? "
            "ORDER BY m.message_id DESC LIMIT ?",
            (ns, before_id, limit),
        )
    else:
        messages = await db.fetchall(
            "SELECT m.message_id, m.sender_name, m.content, m.created_at, "
            "c.name AS channel_name, m.namespace "
            "FROM messages m "
            "JOIN channels c ON c.channel_id = m.channel_id "
            "WHERE m.namespace = ? "
            "ORDER BY m.message_id DESC LIMIT ?",
            (ns, limit),
        )
    messages.reverse()  # chronological order
    msg_list = [
        {
            "id": m["message_id"],
            "sender": m["sender_name"],
            "channel": m["channel_name"],
            "namespace": m["namespace"],
            "content": m["content"],
            "timestamp": ms_to_iso(m["created_at"]),
        }
        for m in messages
    ]

    return JSONResponse({
        "namespace": ns,
        "you": caller["peer_name"],
        "peers": peer_list,
        "channels": channel_list,
        "messages": msg_list,
    })


@mcp.custom_route("/dashboard/api/send", methods=["POST"])
async def dashboard_api_send(request: Request) -> JSONResponse:
    """Send a message from the dashboard. Accepts JSON {channel, content} with Bearer auth."""
    caller = await _authenticate_dashboard_request(request)
    if caller is None:
        return JSONResponse(
            {"error": "Unauthorized", "hint": "Provide a valid Bearer token."},
            status_code=401,
        )
    ns = caller["namespace"]
    me = caller["peer_name"]

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "Invalid JSON body."},
            status_code=400,
        )

    channel = body.get("channel", "").strip()
    content = body.get("content", "").strip()

    if not channel:
        return JSONResponse(
            {"ok": False, "error": "Channel name is required."},
            status_code=400,
        )

    if not CHANNEL_NAME_RE.match(channel):
        return JSONResponse(
            {"ok": False, "error": "Channel name must be 1-64 chars, alphanumeric and hyphens only."},
            status_code=400,
        )

    if not content:
        return JSONResponse(
            {"ok": False, "error": "Message content cannot be empty."},
            status_code=400,
        )

    max_len = CONFIG.get("max_message_length", 50000)
    if len(content) > max_len:
        return JSONResponse(
            {"ok": False, "error": f"Message exceeds maximum length of {max_len} characters."},
            status_code=400,
        )

    # Normalize DM channel names
    channel, dm_error = normalize_channel(channel)
    if dm_error:
        return JSONResponse(
            {"ok": False, "error": dm_error},
            status_code=400,
        )

    # Auto-create channel
    await db.execute(
        "INSERT OR IGNORE INTO channels (namespace, name, created_by, created_at) "
        "VALUES (?, ?, ?, ?)",
        (ns, channel, me, now_ms()),
    )

    ch = await db.fetchone(
        "SELECT channel_id FROM channels WHERE namespace = ? AND name = ?",
        (ns, channel),
    )
    if not ch:
        return JSONResponse(
            {"ok": False, "error": "Failed to create channel."},
            status_code=500,
        )

    now = now_ms()
    cursor = await db.execute(
        "INSERT INTO messages (channel_id, sender_name, namespace, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ch["channel_id"], me, ns, content, now),
    )
    message_id = cursor.lastrowid

    log.info(
        "Dashboard send: %s/%s -> %s (id=%d, len=%d)",
        ns, me, channel, message_id, len(content),
    )

    return JSONResponse({
        "ok": True,
        "message_id": message_id,
        "channel": channel,
        "timestamp": ms_to_iso(now),
    })


# -- Entry Point (Server) -----------------------------------------


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the relay server."""
    global CONFIG
    CONFIG = load_config()
    validate_config(CONFIG)

    # Configure FastMCP transport security:
    # - The default DNS-rebinding protection only allows localhost variants.
    # - When the relay is behind a tunnel/reverse proxy with a custom hostname,
    #   that host arrives in the Host header and gets rejected with HTTP 421.
    # - If `public_url` is set in config, derive its hostname and include it
    #   in `allowed_hosts` (alongside the localhost defaults).
    from mcp.server.fastmcp.server import TransportSecuritySettings
    from urllib.parse import urlparse

    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost"]
    allowed_origins = [
        "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
        "https://127.0.0.1:*", "https://localhost:*",
    ]
    public_url = CONFIG.get("public_url")
    if public_url:
        parsed = urlparse(public_url)
        if parsed.hostname:
            host_with_port = parsed.netloc  # includes port if specified
            allowed_hosts.append(host_with_port)
            allowed_hosts.append(parsed.hostname)  # without port (Host: header may omit it)
            allowed_origins.append(f"{parsed.scheme}://{host_with_port}")
            allowed_origins.append(f"{parsed.scheme}://{parsed.hostname}")
            log.info("Allowing public_url host in transport security: %s", parsed.netloc)

    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )

    async def _run() -> None:
        await db.connect(CONFIG["db_path"])

        app = mcp.streamable_http_app()
        app = TokenAuthMiddleware(app)

        import uvicorn

        config = uvicorn.Config(
            app,
            host=CONFIG["host"],
            port=CONFIG["port"],
            log_level="info",
        )
        server = uvicorn.Server(config)
        log.info("Starting claude-relay on %s:%d", CONFIG["host"], CONFIG["port"])
        await server.serve()

    asyncio.run(_run())


# -- CLI: Token Management ----------------------------------------


def cmd_init(args: argparse.Namespace) -> None:
    """Interactive first-time setup."""
    import sqlite3

    print("Claude Relay -- first-time setup\n")

    port = input("Port [4444]: ").strip() or "4444"
    namespace = input("Default namespace [default]: ").strip() or "default"

    config = dict(DEFAULT_CONFIG)
    config["port"] = int(port)

    Path("data").mkdir(exist_ok=True)
    with open("relay.config.json", "w") as f:
        json.dump(config, f, indent=2)

    conn = sqlite3.connect(config["db_path"])
    try:
        conn.executescript(SCHEMA_SQL)
        conn.execute("UPDATE peers SET status = 'offline'")

        # Create operator's peer token
        raw_token = f"relay_tok_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        conn.execute(
            "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash, "admin", namespace, now_ms()),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"\nConfig written to: relay.config.json")
    print(f"Database created at: {config['db_path']}")
    print(f"\nYour token (SAVE THIS -- shown only once):")
    print(f"  {raw_token}")
    print(f"\nNote: All relay administration is via the CLI on this machine.")
    print(f"\nTo create a peer token:")
    print(f"  python relay.py token create --name shubham --namespace {namespace}")
    print(f"\nTo start the server:")
    print(f"  python relay.py serve")


def cmd_token_create(args: argparse.Namespace) -> None:
    """Generate a new peer token."""
    import sqlite3

    config = load_config()
    conn = sqlite3.connect(config["db_path"])

    name = args.name
    namespace = args.namespace

    if not PEER_NAME_RE.match(name):
        print(
            f"Error: peer name must match {PEER_NAME_RE.pattern}", file=sys.stderr
        )
        sys.exit(1)

    raw_token = f"relay_tok_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    try:
        conn.execute(
            "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash, name, namespace, now_ms()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        print(
            "Error: could not create token (hash collision -- extremely unlikely, try again)",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        conn.close()

    relay_url = args.url if hasattr(args, "url") and args.url else None

    print(f"\nToken for '{name}' in namespace '{namespace}':")
    print(f"  {raw_token}")

    if relay_url:
        join_link = f"{relay_url.rstrip('/')}/join/{raw_token}"
        print(f"\nSend this link to {name}:")
        print(f"  {join_link}")
        print(f"\nThey open it in a browser, copy one command, done.")
    else:
        print(f"\nGive them this command to connect:")
        print(f"  claude mcp add --transport http \\")
        print(f'    --header "Authorization: Bearer {raw_token}" \\')
        print(f"    -- relay https://YOUR_RELAY_HOST/mcp")
        print(f"\n  (the `--` is REQUIRED -- --header is variadic and will")
        print(f"   otherwise eat the positional 'relay' argument)")
        print(f"\n  Tip: use --url to generate a clickable join link:")
        print(f"  python relay.py token create --name {name} --url https://relay.example.com")

    print(f"\nPost-setup: tell them to say 'check the relay' in Claude Code.")


def cmd_token_list(args: argparse.Namespace) -> None:
    """List all tokens (shows hashes and peer names, not raw tokens)."""
    import sqlite3

    config = load_config()
    conn = sqlite3.connect(config["db_path"])
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT token_hash, peer_name, namespace, created_at, last_used_at "
        "FROM tokens ORDER BY namespace, peer_name"
    ).fetchall()
    conn.close()

    if not rows:
        print("No tokens found.")
        return

    print(
        f"{'Peer':<20} {'Namespace':<15} {'Created':<22} "
        f"{'Last Used':<22} {'Hash (first 12)'}"
    )
    print("-" * 100)
    for r in rows:
        created = ms_to_iso(r["created_at"]) if r["created_at"] else "never"
        used = ms_to_iso(r["last_used_at"]) if r["last_used_at"] else "never"
        print(
            f"{r['peer_name']:<20} {r['namespace']:<15} {created:<22} "
            f"{used:<22} {r['token_hash'][:12]}..."
        )


def cmd_token_revoke(args: argparse.Namespace) -> None:
    """Revoke a token by deleting its row. Also cleans up cursors."""
    import sqlite3

    config = load_config()
    conn = sqlite3.connect(config["db_path"])

    name = args.name
    namespace = args.namespace

    deleted = conn.execute(
        "DELETE FROM tokens WHERE peer_name = ? AND namespace = ?",
        (name, namespace),
    ).rowcount

    if deleted == 0:
        print(
            f"No token found for '{name}' in namespace '{namespace}'.",
            file=sys.stderr,
        )
        conn.close()
        sys.exit(1)

    conn.execute(
        "DELETE FROM cursors WHERE peer_name = ? AND namespace = ?",
        (name, namespace),
    )
    conn.commit()
    conn.close()

    print(f"Token for '{name}' in namespace '{namespace}' has been revoked.")
    print("Cursors cleaned up. The peer can no longer authenticate.")
    print(f"\nTo re-create a token for this peer:")
    print(f"  python relay.py token create --name {name} --namespace {namespace}")


def cmd_check(args: argparse.Namespace) -> None:
    """Verify deployment by checking config, DB, and optionally the HTTP endpoint."""
    import sqlite3

    print("Claude Relay -- deployment check\n")

    config_path = Path("relay.config.json")
    if not config_path.exists():
        print("[FAIL] relay.config.json not found. Run: python relay.py init")
        sys.exit(1)
    print("[OK]   relay.config.json found")

    config = load_config()
    try:
        validate_config(config)
        print("[OK]   Config validation passed")
    except ValueError as e:
        print(f"[FAIL] Config validation: {e}")
        sys.exit(1)

    db_path = Path(config["db_path"])
    if not db_path.exists():
        print(f"[FAIL] Database not found at {db_path}. Run: python relay.py init")
        sys.exit(1)
    print(f"[OK]   Database found: {db_path} ({db_path.stat().st_size} bytes)")

    conn = sqlite3.connect(str(db_path))
    token_count = conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
    peer_count = conn.execute("SELECT COUNT(*) FROM peers").fetchone()[0]
    conn.close()
    print(f"[OK]   {token_count} token(s), {peer_count} peer(s)")

    if args.url:
        try:
            import urllib.request

            resp = urllib.request.urlopen(f"{args.url}/health", timeout=5)
            data = json.loads(resp.read())
            if data.get("status") == "ok":
                print(f"[OK]   Server responding at {args.url}/health")
            else:
                print(f"[WARN] Server responded but status is not 'ok': {data}")
        except Exception as e:
            print(f"[FAIL] Could not reach server at {args.url}/health: {e}")
    else:
        print("[SKIP] No --url provided, skipping server connectivity check")

    print("\nDeployment check complete.")


# -- CLI Argument Parsing -----------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="relay",
        description="Claude Relay -- message relay server for Claude Code instances",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init", help="First-time setup")
    subparsers.add_parser("serve", help="Start the relay server")

    token_parser = subparsers.add_parser("token", help="Token management")
    token_sub = token_parser.add_subparsers(dest="token_command")

    tc = token_sub.add_parser("create", help="Create a peer token")
    tc.add_argument("--name", required=True, help="Peer name")
    tc.add_argument(
        "--namespace", default="default", help="Namespace (default: 'default')"
    )
    tc.add_argument(
        "--url", help="Relay URL (e.g. https://relay.example.com) to generate a join link"
    )

    token_sub.add_parser("list", help="List all tokens")

    tr = token_sub.add_parser("revoke", help="Revoke a peer token")
    tr.add_argument("--name", required=True, help="Peer name")
    tr.add_argument(
        "--namespace", default="default", help="Namespace (default: 'default')"
    )

    chk = subparsers.add_parser("check", help="Verify deployment")
    chk.add_argument(
        "--url", help="Server URL to test (e.g. https://relay.example.com)"
    )

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "token":
        if args.token_command == "create":
            cmd_token_create(args)
        elif args.token_command == "list":
            cmd_token_list(args)
        elif args.token_command == "revoke":
            cmd_token_revoke(args)
        else:
            token_parser.print_help()
    elif args.command == "check":
        cmd_check(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
