"""Dashboard routes: /dashboard, /dashboard/api, /dashboard/api/send, and /join page."""

import hashlib
import logging
import secrets
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .server import mcp
from .config import CONFIG, now_ms, ms_to_iso
from . import db as _db_mod
from .channels import CHANNEL_NAME_RE, PEER_NAME_RE, normalize_channel

log = logging.getLogger("relay")

_STATIC_DIR = Path(__file__).parent.parent.parent / "static"


def _load_template(name: str) -> str:
    return (_STATIC_DIR / name).read_text()


# -- Join Page -----------------------------------------------------


@mcp.custom_route("/join/{token}", methods=["GET"])
async def join_page(request: Request) -> Response:
    token = request.path_params.get("token", "")
    if not token.startswith("relay_tok_"):
        return HTMLResponse("<h1>Invalid token</h1>", status_code=400)

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = await _db_mod.db.fetchone(
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
        f'claude mcp add --transport http --scope user '
        f'-H "Authorization: Bearer {token}" '
        f"-- relay {relay_url}"
    )

    # Build the dashboard URL for the join page
    configured_base = CONFIG.get("public_url") if CONFIG else None
    if configured_base:
        dashboard_url = configured_base.rstrip("/") + "/dashboard"
    else:
        dashboard_url = f"{request.url.scheme}://{request.url.netloc}/dashboard"

    html = _load_template("join.html").format(
        relay_name=row["namespace"],
        peer_name=row["peer_name"],
        mcp_command=mcp_command,
        dashboard_url=dashboard_url,
    )
    return HTMLResponse(html)


# -- Dashboard -----------------------------------------------------


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request: Request) -> Response:
    return HTMLResponse(_load_template("dashboard.html"))


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
    row = await _db_mod.db.fetchone(
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
    peers = await _db_mod.db.fetchall(
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
    channels = await _db_mod.db.fetchall(
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
        messages = await _db_mod.db.fetchall(
            "SELECT m.message_id, m.sender_name, m.content, m.created_at, "
            "c.name AS channel_name, m.namespace "
            "FROM messages m "
            "JOIN channels c ON c.channel_id = m.channel_id "
            "WHERE m.namespace = ? AND m.message_id < ? "
            "ORDER BY m.message_id DESC LIMIT ?",
            (ns, before_id, limit),
        )
    else:
        messages = await _db_mod.db.fetchall(
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
        return JSONResponse(
            {"ok": False, "error": "Failed to create channel."},
            status_code=500,
        )

    now = now_ms()
    cursor = await _db_mod.db.execute(
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


@mcp.custom_route("/dashboard/api/invite", methods=["POST"])
async def dashboard_api_invite(request: Request) -> JSONResponse:
    """Create an invite link for a new peer. Requires Bearer auth."""
    caller = await _authenticate_dashboard_request(request)
    if caller is None:
        return JSONResponse(
            {"error": "Unauthorized", "hint": "Provide a valid Bearer token."},
            status_code=401,
        )
    ns = caller["namespace"]

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "Invalid JSON body."},
            status_code=400,
        )

    name = body.get("name", "").strip()
    if not name:
        return JSONResponse(
            {"ok": False, "error": "Peer name is required."},
            status_code=400,
        )

    if not PEER_NAME_RE.match(name):
        return JSONResponse(
            {"ok": False, "error": "Peer name must be 1-32 chars, starting with alphanumeric, then alphanumeric/underscore/hyphen."},
            status_code=400,
        )

    # Generate token and hash
    raw_token = f"relay_tok_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    # Insert into tokens table
    try:
        await _db_mod.db.execute(
            "INSERT INTO tokens (token_hash, peer_name, namespace, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash, name, ns, now_ms()),
        )
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "Failed to create token (hash collision or database error)."},
            status_code=500,
        )

    # Build relay URL
    configured = CONFIG.get("public_url") if CONFIG else None
    if configured:
        base_url = configured.rstrip("/")
    else:
        base_url = f"{request.url.scheme}://{request.url.netloc}"

    join_link = f"{base_url}/join/{raw_token}"
    relay_url = f"{base_url}/mcp"
    mcp_command = (
        f'claude mcp add --transport http --scope user '
        f'-H "Authorization: Bearer {raw_token}" '
        f"-- relay {relay_url}"
    )

    log.info("Invite created: %s/%s by %s", ns, name, caller["peer_name"])

    return JSONResponse({
        "ok": True,
        "peer_name": name,
        "token": raw_token,
        "join_link": join_link,
        "mcp_command": mcp_command,
    })


@mcp.custom_route("/dashboard/api/me", methods=["GET"])
async def dashboard_api_me(request: Request) -> JSONResponse:
    """Return info about the authenticated caller."""
    caller = await _authenticate_dashboard_request(request)
    if caller is None:
        return JSONResponse(
            {"error": "Unauthorized", "hint": "Provide a valid Bearer token."},
            status_code=401,
        )

    # Extract the raw token from the Authorization header
    auth = request.headers.get("authorization", "")
    raw_token = auth[7:].strip()  # already validated by _authenticate_dashboard_request

    # Build relay URL
    configured = CONFIG.get("public_url") if CONFIG else None
    if configured:
        relay_url = configured.rstrip("/")
    else:
        relay_url = f"{request.url.scheme}://{request.url.netloc}"

    mcp_url = f"{relay_url}/mcp"
    mcp_command = (
        f'claude mcp add --transport http --scope user '
        f'-H "Authorization: Bearer {raw_token}" '
        f"-- relay {mcp_url}"
    )

    return JSONResponse({
        "peer_name": caller["peer_name"],
        "namespace": caller["namespace"],
        "relay_url": relay_url,
        "mcp_command": mcp_command,
    })


__all__ = [
    "join_page",
    "dashboard",
    "dashboard_api",
    "dashboard_api_send",
    "dashboard_api_invite",
    "dashboard_api_me",
]
