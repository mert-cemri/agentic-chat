"""MCP tool implementations: heartbeat, send, receive, list_peers, list_channels."""

import logging
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context
from starlette.requests import Request
from starlette.responses import JSONResponse

from .server import mcp
from .auth import get_caller
from .config import CONFIG, now_ms, ms_to_iso
from . import db as _db_mod
from .channels import CHANNEL_NAME_RE, is_dm_channel, normalize_channel
from .cleanup import maybe_cleanup

log = logging.getLogger("relay")


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
            await _db_mod.db.execute(
                """UPDATE peers SET status='online', last_heartbeat=?,
                   last_heartbeat_monotonic=?, status_message=?
                   WHERE namespace=? AND peer_name=?""",
                (now, mono, status_message, ns, me),
            )
        else:
            await _db_mod.db.execute(
                """UPDATE peers SET status='online', last_heartbeat=?,
                   last_heartbeat_monotonic=?
                   WHERE namespace=? AND peer_name=?""",
                (now, mono, ns, me),
            )

        # Mark stale peers offline
        timeout = CONFIG.get("heartbeat_timeout_seconds", 120)
        cutoff_mono = mono - timeout
        await _db_mod.db.execute(
            "UPDATE peers SET status='offline' "
            "WHERE namespace=? AND status='online' AND last_heartbeat_monotonic < ?",
            (ns, cutoff_mono),
        )

        # Get peer list
        peers = await _db_mod.db.fetchall(
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
        unread_rows = await _db_mod.db.fetchall(
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
        await _db_mod.db.execute(
            "INSERT OR IGNORE INTO channels (namespace, name, created_by, created_at) "
            "VALUES (?, ?, ?, ?)",
            (ns, channel, me, now_ms()),
        )

        ch = await _db_mod.db.fetchone(
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
        cursor = await _db_mod.db.execute(
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
            rows = await _db_mod.db.fetchall(
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
                    await _db_mod.db.execute(
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

        ch = await _db_mod.db.fetchone(
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
            cursor_row = await _db_mod.db.fetchone(
                "SELECT last_read_id FROM cursors "
                "WHERE namespace = ? AND peer_name = ? AND channel_id = ?",
                (ns, me, channel_id),
            )
            start_cursor = cursor_row["last_read_id"] if cursor_row else 0

        rows = await _db_mod.db.fetchall(
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
            await _db_mod.db.execute(
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
        await _db_mod.db.execute(
            "UPDATE peers SET status='offline' "
            "WHERE namespace=? AND status='online' AND last_heartbeat_monotonic < ?",
            (ns, mono - timeout),
        )

        peers = await _db_mod.db.fetchall(
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
        rows = await _db_mod.db.fetchall(
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
    if _db_mod.db.path:
        try:
            db_size = Path(_db_mod.db.path).stat().st_size
        except OSError:
            pass
    return JSONResponse(
        {"status": "ok", "server": "claude-relay", "db_size_bytes": db_size}
    )


__all__ = ["heartbeat", "send", "receive", "list_peers", "list_channels", "health"]
