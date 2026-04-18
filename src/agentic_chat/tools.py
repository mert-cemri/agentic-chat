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
    """Check in with the relay, see who is online, and get unread message counts per channel.

    WHEN TO CALL THIS TOOL:
    - User says "any messages?", "check the relay", "what's happening?", "relay status"
    - User says "who's online?", "who's around?" (this tool covers that too, but list_peers gives more detail)
    - As the FIRST step before calling receive -- heartbeat tells you WHERE unreads are so you know which channels to read
    - Anytime the user asks you to check in or see what is new

    WHAT IT RETURNS:
    - your identity (peer_name, namespace) -- confirms who you are on the relay
    - a list of other peers with their online/offline status and optional status messages
    - an unread_summary with total_unread count and a per-channel breakdown of unread counts
    - calling this also marks you as "online" so other peers can see you are active

    WHEN TO USE THIS VS OTHER TOOLS:
    - Use heartbeat FIRST, then call receive if unread counts are > 0
    - If the user only wants to know about people (not messages), list_peers is more focused
    - If the user wants to read actual message content, you need receive after this

    PARAMETERS:
    - status_message (optional): a short string (max 200 chars) that other peers see, e.g. "working on auth module", "in a meeting"

    EXAMPLES:
    - User says "any messages?" -> call heartbeat, check unread_summary, then call receive if total_unread > 0
    - User says "check the relay" -> call heartbeat, summarize peers online and unread counts
    - User says "set my status to working on frontend" -> call heartbeat with status_message="working on frontend"
    - User says "who's online?" -> call heartbeat (or list_peers), report the peers list
    """
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

        # Welcome message for first-time peers
        msg_count = await _db_mod.db.fetchone(
            "SELECT COUNT(*) as c FROM messages WHERE sender_name = ? AND namespace = ?",
            (me, ns),
        )
        peer_row = await _db_mod.db.fetchone(
            "SELECT last_heartbeat FROM peers WHERE namespace = ? AND peer_name = ?",
            (ns, me),
        )
        is_first_heartbeat = (
            msg_count is not None
            and msg_count["c"] == 0
            and peer_row is not None
            and peer_row["last_heartbeat"] == now
        )
        if is_first_heartbeat:
            await _db_mod.db.execute(
                "INSERT OR IGNORE INTO channels (namespace, name, created_by, created_at) "
                "VALUES (?, 'general', ?, ?)",
                (ns, me, now),
            )
            welcome_ch = await _db_mod.db.fetchone(
                "SELECT channel_id FROM channels WHERE namespace = ? AND name = 'general'",
                (ns,),
            )
            if welcome_ch:
                public_url = CONFIG.get("public_url")
                dashboard_line = (
                    f"\nThe dashboard is at {public_url}/dashboard"
                    if public_url
                    else ""
                )
                await _db_mod.db.execute(
                    "INSERT INTO messages (channel_id, sender_name, sender_display_name, "
                    "namespace, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        welcome_ch["channel_id"],
                        "system",
                        "Relay Bot",
                        ns,
                        f"Welcome {me}! You're connected to the relay.\n\n"
                        f"Try these:\n"
                        f"- \"who's online?\" -- see other peers\n"
                        f"- \"send hello to general\" -- broadcast a message\n"
                        f"- \"tell alice I'm here\" -- send a DM\n"
                        f"- \"check messages\" -- read your unreads"
                        f"{dashboard_line}",
                        now,
                    ),
                )
                log.info("Sent welcome message for new peer %s/%s", ns, me)

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
    display_name: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict:
    """Send a message to a channel or DM on the relay. The channel is auto-created if it does not exist yet.

    WHEN TO CALL THIS TOOL:
    - User says "tell X ...", "message X ...", "send X ...", "reply to X ..."
    - User says "broadcast ...", "tell everyone ...", "announce ..."
    - User says "respond to X with ..." or "let X know ..."

    HOW TO CONSTRUCT THE CHANNEL NAME:
    - For a DM to a specific person: channel="dm-yourname-theirname" (the server sorts the two names alphabetically, so the order you provide does not matter)
    - For a broadcast to everyone: channel="general"
    - For a topic channel: channel="some-topic-name" (alphanumeric and hyphens only, 1-64 chars)
    - Your identity (the sender name) is determined automatically from your auth token -- you never need to ask the user who they are

    WHAT IT RETURNS:
    - ok: whether the message was sent successfully
    - message_id: the ID of the sent message
    - channel: the normalized channel name
    - timestamp: when the message was created

    PARAMETERS:
    - channel (required): the channel to send to -- see naming conventions above
    - content (required): the message text to send (max 50000 chars)

    EXAMPLES:
    - User says "tell alice I pushed the fix" -> call send(channel="dm-yourname-alice", content="I pushed the fix")
    - User says "broadcast: standup in 5 minutes" -> call send(channel="general", content="standup in 5 minutes")
    - User says "reply to bob with sounds good" -> call send(channel="dm-yourname-bob", content="sounds good")
    - User says "tell everyone the deploy is done" -> call send(channel="general", content="the deploy is done")
    - User says "message the frontend team: PR is ready" -> call send(channel="frontend-team", content="PR is ready")
    """
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
        # display_name defaults to "{peer_name} (claude)" for agent sessions
        effective_display = display_name or f"{me} (claude)"
        cursor = await _db_mod.db.execute(
            "INSERT INTO messages (channel_id, sender_name, sender_display_name, namespace, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ch["channel_id"], me, effective_display, ns, content, now),
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
    """Read messages from the relay -- either from a specific channel/DM or from all channels at once.

    WHEN TO CALL THIS TOOL:
    - User says "check messages", "read messages", "what did X say?", "show me the conversation"
    - User says "check messages from X", "any messages from alice?", "what's in #general?"
    - After calling heartbeat and seeing unread counts > 0 -- this is how you fetch the actual message content
    - User says "read the conversation with bob", "show me what happened in general"

    WHAT IT RETURNS:
    - A list of messages, each with: id, from (sender name), content, timestamp, and channel (if reading all channels)
    - count: how many messages were returned
    - has_more: whether there are additional unread messages beyond the limit
    - By default, only UNREAD messages are returned and reading them marks them as read (advances your cursor)

    WHEN TO USE THIS VS OTHER TOOLS:
    - Call heartbeat FIRST to see where unreads are, then call receive to fetch the actual messages
    - If the user asks "any messages?" do heartbeat first, then receive only if unreads > 0
    - If the user wants to send a message, use the send tool instead

    PARAMETERS:
    - channel (optional): omit to get unread messages from ALL channels at once. Set to a specific channel name like "general" or "dm-yourname-theirname" to read only that channel
    - limit (optional, default 20, max 100): how many messages to fetch
    - peek (optional, default false): if true, read messages WITHOUT marking them as read -- useful for previewing
    - since_id (optional): fetch messages after this message ID regardless of your read cursor -- useful for re-reading history. Requires a specific channel

    EXAMPLES:
    - User says "check messages" -> call receive() with no channel to get all unreads
    - User says "what did alice say?" -> call receive(channel="dm-yourname-alice")
    - User says "show me the last 50 messages in general" -> call receive(channel="general", limit=50, since_id=0)
    - User says "peek at messages without marking read" -> call receive(peek=true)
    - User says "read messages from bob" -> call receive(channel="dm-yourname-bob")
    """
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
                """SELECT m.message_id, m.sender_name, m.sender_display_name, m.content, m.created_at,
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
                        "from": r["sender_display_name"] or r["sender_name"],
                        "from_peer": r["sender_name"],
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
            """SELECT message_id, sender_name, sender_display_name, content, created_at
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
                    "from": r["sender_display_name"] or r["sender_name"],
                    "from_peer": r["sender_name"],
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
    """List all peers (other Claude Code instances or users) on the relay with their online/offline status.

    WHEN TO CALL THIS TOOL:
    - User says "who's on the relay?", "who's online?", "list peers", "who's around?", "show me who's connected"
    - User asks about people or participants rather than messages
    - User wants to know if a specific person is available before messaging them

    WHAT IT RETURNS:
    - A list of all known peers with: name, status (online/offline), status_message (if set), last_seen timestamp, and last_seen_seconds_ago
    - Total count and online count

    WHEN TO USE THIS VS OTHER TOOLS:
    - Use this when the user is asking about PEOPLE, not messages
    - heartbeat also returns a peer list, but list_peers is more focused and does not update your own status
    - If the user wants messages, use heartbeat + receive instead

    EXAMPLES:
    - User says "who's online?" -> call list_peers, report who is online and who is offline
    - User says "is alice around?" -> call list_peers, check alice's status in the result
    - User says "who's on the relay?" -> call list_peers, summarize the peer list
    """
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
    """List all channels on the relay with unread counts, message totals, and last activity.

    WHEN TO CALL THIS TOOL:
    - User says "what channels are there?", "show channels", "list channels", "what groups exist?"
    - User wants an overview of all available channels before reading or sending
    - User asks "where is the conversation happening?" or "which channels have activity?"

    WHAT IT RETURNS:
    - A list of all channels with: name, unread count, total_messages, last_activity timestamp, and last_sender
    - Total number of channels

    WHEN TO USE THIS VS OTHER TOOLS:
    - Use this for channel discovery -- to see what channels exist and which have unread messages
    - If the user wants to read actual messages, use receive after identifying the channel
    - heartbeat also shows unread counts per channel, but list_channels gives more detail (total messages, last sender, last activity)

    EXAMPLES:
    - User says "what channels are there?" -> call list_channels, list the channel names and their unread counts
    - User says "show me active channels" -> call list_channels, highlight channels with recent activity
    """
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
